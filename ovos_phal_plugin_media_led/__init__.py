# ovos_phal_plugin_media_led/__init__.py
import colorsys
import time
import threading
import atexit
import signal
from json_database import JsonConfigXDG
from ovos_plugin_manager.phal import PHALPlugin
from ovos_utils import create_daemon
from ovos_utils.log import LOG

class NullLED:
    def __init__(self, name): self.name = name
    def __bool__(self): return False
    def fill(self, *args, **kwargs): pass
    def __setitem__(self, i, color): pass
    def set_pixel(self, *args, **kwargs): pass
    def show(self): pass
    def clear(self): pass
    def close(self): pass
    num_pixels = 0


# --- Helpers ---
def resolve_board_pin(pin_like):
    """
    Accepts: a board.* object, a string like 'D18'/'SCK'/'MOSI', or an int.
    Returns: a board pin object when possible, else raises.
    """
    try:
        import board
    except Exception as e:
        raise RuntimeError(f"board module unavailable: {e}")

    # Already a board pin object?
    if hasattr(pin_like, "__module__") and getattr(pin_like, "__module__", "").endswith(".board"):
        return pin_like

    # String names -> board attributes
    if isinstance(pin_like, str):
        # Allow raw names like "D18", "SCK", "MOSI", etc.
        if hasattr(board, pin_like):
            return getattr(board, pin_like)
        # Also allow lowercase
        up = pin_like.upper()
        if hasattr(board, up):
            return getattr(board, up)
        raise ValueError(f"Unknown board pin name: {pin_like}")

    # Ints: treat like D{num}
    if isinstance(pin_like, int):
        name = f"D{pin_like}"
        if hasattr(board, name):
            return getattr(board, name)
        raise ValueError(f"Cannot resolve integer pin {pin_like} to board.{name}")

    raise TypeError(f"Unsupported pin type: {type(pin_like)} ({pin_like})")


# --- Drivers ---
def make_dotstar(num, data, clock, brightness):
    try:
        import adafruit_dotstar as dotstar
        clk = resolve_board_pin(clock)
        dat = resolve_board_pin(data)
        leds = dotstar.DotStar(clk, dat, n=num, brightness=brightness, auto_write=False)
        leds.num_pixels = num
        atexit.register(lambda: (safe_clear(leds), safe_deinit(leds)))
        return leds
    except Exception as e:
        LOG.debug(f"DotStar unavailable: {e}")
        return NullLED("dotstar")


def make_neopixel(num, pin, brightness, order=None):
    try:
        import neopixel
        pin_obj = resolve_board_pin(pin)
        kwargs = dict(brightness=brightness, auto_write=False)
        if order is None:
            order = getattr(neopixel, "GRB", None)
        if order is not None:
            kwargs["pixel_order"] = order
        pixels = neopixel.NeoPixel(pin_obj, num, **kwargs)
        pixels.num_pixels = num
        atexit.register(lambda: (safe_clear(pixels), safe_deinit(pixels)))
        return pixels
    except Exception as e:
        LOG.debug(f"NeoPixel unavailable: {e}")
        return NullLED("neopixel")


def make_ledshim(num, brightness):
    """Wrap Pimoroni LED SHIM in a DotStar/NeoPixel-like interface."""
    try:
        import ledshim
        ledshim.set_brightness(brightness)
        ledshim.set_clear_on_exit(True)

        class LEDShimWrapper:
            num_pixels = num
            def __bool__(self): return True
            def fill(self, color):
                r, g, b = color
                for i in range(self.num_pixels):
                    ledshim.set_pixel(i, r, g, b)
            def __setitem__(self, i, color):
                r, g, b = color
                if 0 <= i < self.num_pixels:
                    ledshim.set_pixel(i, r, g, b)
            def show(self): ledshim.show()
            def clear(self): ledshim.clear(); ledshim.show()
            def close(self): pass

        wrapper = LEDShimWrapper()
        atexit.register(lambda: wrapper.clear())
        return wrapper

    except Exception as e:
        LOG.debug(f"LED SHIM unavailable: {e}")
        return NullLED("ledshim")


def safe_clear(dev):
    try:
        try:
            dev.fill((0, 0, 0))
        except Exception:
            dev.clear()
        dev.show()
    except Exception:
        pass


def safe_deinit(dev):
    for m in ("deinit", "close"):
        try:
            getattr(dev, m)()
            return
        except Exception:
            pass


class MultiLED:
    """Fan-out writes to all available backends; absent ones no-op."""
    def __init__(self, drivers):
        self.drivers = [d for d in drivers if d]
        self._lock = threading.RLock()
        atexit.register(self.clear)

    @property
    def num_pixels(self):
        return max((getattr(d, "num_pixels", 0) for d in self.drivers), default=0)

    def fill(self, color):
        with self._lock:
            for d in self.drivers:
                try:
                    d.fill(color)
                except Exception:
                    pass
            self.show()

    def __setitem__(self, i, color):
        with self._lock:
            for d in self.drivers:
                try:
                    d[i] = color
                except Exception:
                    try:
                        d.__setitem__(i, color)
                    except Exception:
                        try:
                            d.set_pixel(i, color)
                        except Exception:
                            pass

    def show(self):
        with self._lock:
            for d in self.drivers:
                try:
                    d.show()
                except Exception:
                    pass

    def clear(self):
        with self._lock:
            for d in self.drivers:
                try:
                    try:
                        d.fill((0, 0, 0))
                    except Exception:
                        d.clear()
                    d.show()
                except Exception:
                    pass

    def close(self):
        # Clear before deinit/close so the last frame is definitely "off"
        with self._lock:
            try:
                try:
                    for d in self.drivers:
                        try:
                            d.fill((0, 0, 0))
                        except Exception:
                            d.clear()
                    for d in self.drivers:
                        try:
                            d.show()
                        except Exception:
                            pass
                except Exception:
                    pass
            finally:
                for d in self.drivers:
                    try:
                        d.close()
                    except Exception:
                        try:
                            d.deinit()
                        except Exception:
                            pass


class MediaLedPlugin(PHALPlugin):
    """
    Behavior:
      - When media starts/resumes playing -> start rainbow animation
      - When media pauses/stops          -> stop animation and clear LEDs
    Listens to both OCP command events and OCP player state events.
    """
    def __init__(self, bus=None, config=None):
        super().__init__(bus=bus, name="ovos-phal-plugin-media-led", config=config)

        # Settings: ~/.config/OpenVoiceOS/ovos-phal-plugin-media-led.json
        self.settings = JsonConfigXDG(self.name, subfolder="OpenVoiceOS")

        # Feature flags
        self.use_dotstar   = bool(self.settings.get("use_dotstar", False))
        self.use_neopixel  = bool(self.settings.get("use_neopixel", False))
        self.use_ledshim   = bool(self.settings.get("use_shim", False))

        # Global brightness (0..1)
        self.brightness = float(self.settings.get("brightness", 0.3))
        self.fps = int(self.settings.get("fps", 60))

        # NeoPixel single-pin (default D18)
        gpio_pin_num = self.settings.get("gpio_pin", 18)  # sensible default
        self.pin = f"D{gpio_pin_num}"

        # DotStar data/clock (defaults to SPI0: MOSI/SCK)
        self.ds_data_pin  = self.settings.get("dotstar_data_pin", "MOSI")
        self.ds_clock_pin = self.settings.get("dotstar_clock_pin", "SCK")

        # LED counts
        self.ds_num_leds   = int(self.settings.get("dotstar_num_leds", 0))
        self.np_num_leds   = int(self.settings.get("neopixel_num_leds", 0))
        self.shim_num_leds = int(self.settings.get("ledshim_num_leds", 0))

        # Runtime state
        self.playing = False
        self._anim_thread = None
        self._stop_event = threading.Event()
        self._shutting_down = False

        # === Safe LED init (honor feature flags) ===
        drivers = []

        if self.use_dotstar:
            drivers.append(make_dotstar(
                num=self.ds_num_leds,
                data=self.ds_data_pin,
                clock=self.ds_clock_pin,
                brightness=self.brightness
            ))
        if self.use_neopixel:
            drivers.append(make_neopixel(
                num=self.np_num_leds,
                pin=self.pin,
                brightness=self.brightness
            ))
        if self.use_ledshim:
            drivers.append(make_ledshim(
                num=self.shim_num_leds,
                brightness=self.brightness
            ))

        self.leds = MultiLED(drivers)

        if self.leds.num_pixels == 0 and not self.leds.drivers:
            LOG.info("MediaLedPlugin: no LED drivers available; plugin will no-op.")
        else:
            LOG.info(f"MediaLedPlugin: active LED drivers={len(self.leds.drivers)}, pixels={self.leds.num_pixels}")

        atexit.register(self.shutdown)
        # Best-effort cleanup if the process receives a termination signal
        try:  # signal handlers must be in the main thread; ignore if not
            def _sig_handler(signum, frame):
                try:
                    LOG.info(f"MediaLedPlugin: received signal {signum}, shutting down")
                except Exception:
                    pass
                self.shutdown()
            signal.signal(signal.SIGTERM, _sig_handler)  # <-- add
            signal.signal(signal.SIGINT, _sig_handler)   # <-- add
        except Exception:
            pass

        # ---- Event subscriptions ----
        self.bus.on("ovos.common_play.play", self._handle_playing_started)
        self.bus.on("ovos.common_play.resume", self._handle_playing_started)
        self.bus.on("ovos.common_play.pause", self._handle_playing_stopped)
        self.bus.on("ovos.common_play.stop", self._handle_playing_stopped)
        self.bus.on("ovos.common_play.player.state", self._on_player_state)
        # Legacy
        self.bus.on("mycroft.audio.service.play", self._handle_playing_started)
        self.bus.on("mycroft.audio.service.resume", self._handle_playing_started)
        self.bus.on("mycroft.audio.service.pause", self._handle_playing_stopped)
        self.bus.on("mycroft.audio.service.stop", self._handle_playing_stopped)

    # ---------- Event handlers ----------
    def _on_player_state(self, message):
        state = (message.data or {}).get("state") or ""
        if not isinstance(state, str):
            state = str(state)
        st = state.lower()
        if st == "playing":
            self._handle_playing_started(message)
        elif st in ("paused", "stopped"):
            self._handle_playing_stopped(message)

    def _handle_playing_started(self, _):
        if not self.leds.drivers or self.playing:
            return
        self.playing = True
        self._stop_event.clear()
        self._anim_thread = create_daemon(target=self._rainbow, args=(self._stop_event,))

    def _handle_playing_stopped(self, _):
        if not self.leds.drivers or not self.playing:
            return
        self.playing = False
        self._stop_event.set()

        t = self._anim_thread
        self._anim_thread = None
        if t and t.is_alive():
            try:
                t.join(timeout=1.0)
            except Exception:
                pass

        try:
            self.leds.clear()
        except Exception as e:
            LOG.debug(f"LED clear failed: {e}")

    # ---------- Animation ----------
    def _rainbow(self, stop_event: threading.Event):
        """Rainbow marquee while playing."""
        # If lengths differ, use the max; shorter devices ignore out-of-range writes via try/except
        num = max(1, self.leds.num_pixels or 28)
        spacing = 360.0 / float(num)

        try:
            while not stop_event.is_set() and self.playing:
                hue = int(time.time() * 100) % 360
                for x in range(num):
                    h = ((hue + x * spacing) % 360) / 360.0
                    r, g, b = [int(c * 255) for c in colorsys.hsv_to_rgb(h, 1.0, 1.0)]
                    try:
                        self.leds[x] = (r, g, b)
                    except Exception as e:
                        LOG.debug(f"LED set[{x}] failed: {e}")
                        break
                try:
                    self.leds.show()
                except Exception as e:
                    LOG.debug(f"LED show failed: {e}")
                time.sleep(1.0 / self.fps)
        finally:
            try:
                self.leds.clear()
                self.leds.show()
            except Exception as e:
                LOG.debug(f"LED final clear failed: {e}")

    # ---------- Lifecycle ----------
    def shutdown(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        try:
            self._handle_playing_stopped(None)
            if hasattr(self, "leds"):
                self.leds.clear()
                self.leds.show()
                self.leds.close()
        finally:
            try:
                super().shutdown()
            except Exception:
                pass

