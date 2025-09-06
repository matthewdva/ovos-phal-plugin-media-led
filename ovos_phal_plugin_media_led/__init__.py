"""Pimoroni Ledshim Media LED PHAL plugin"""
import colorsys
import time
import threading
from ovos_plugin_manager.phal import PHALPlugin
from ovos_utils import create_daemon
from ovos_utils.log import LOG

try:
    import ledshim   # Pimoroni LED SHIM (28 LEDs)
    LEDSHIM_OK = True
except ImportError:
    LEDSHIM_OK = False
    LOG.warning("ledshim not installed. `pip install ledshim` to enable LEDs.")


class MediaLedPlugin(PHALPlugin):
    """
    Behavior:
      - When media starts/resumes playing -> start rainbow animation
      - When media pauses/stops          -> stop animation and clear LEDs
    Listens to both OCP command events and OCP player state events.
    """
    def __init__(self, bus=None, config=None):
        super().__init__(bus=bus, name="ovos-phal-plugin-media-led", config=config)

        # runtime state
        self.playing = False
        self._anim_thread = None
        self._stop_event = threading.Event()

        if not LEDSHIM_OK:
            # Still register nothing to avoid errors; plugin becomes a no-op.
            return

        # Safe LED init
        try:
            ledshim.set_clear_on_exit()
            ledshim.set_brightness(0.8)
            ledshim.clear()
            ledshim.show()
        except Exception as e:
            LOG.error(f"Cannot initialize ledshim: {e}")
            return

        # ---- Event subscriptions ----
        # Command intents (fire immediately when user asks)
        self.bus.on("ovos.common_play.play", self._handle_playing_started)
        self.bus.on("ovos.common_play.resume", self._handle_playing_started)
        self.bus.on("ovos.common_play.pause", self._handle_playing_stopped)
        self.bus.on("ovos.common_play.stop", self._handle_playing_stopped)

        # Actual player state (reflects backend transitions: PLAYING/PAUSED/etc.)
        self.bus.on("ovos.common_play.player.state", self._on_player_state)

        # (Optional) Legacy AudioService compatibility
        self.bus.on("mycroft.audio.service.play", self._handle_playing_started)
        self.bus.on("mycroft.audio.service.resume", self._handle_playing_started)
        self.bus.on("mycroft.audio.service.pause", self._handle_playing_stopped)
        self.bus.on("mycroft.audio.service.stop", self._handle_playing_stopped)

    # ---------- Event handlers ----------
    def _on_player_state(self, message):
        # message.data.get("state") typically: "playing" | "paused" | "stopped" | "buffering"
        state = (message.data or {}).get("state") or ""

        # Convert enum to string safely
        if state is None:
            return
        if not isinstance(state, str):
            state = str(state)        # e.g. "PlayerState.PLAYING"

        st = state.lower()

        if st == "playing":
            self._handle_playing_started(message)
        elif st in ("paused", "stopped"):
            self._handle_playing_stopped(message)

    def _handle_playing_started(self, _):
        if not LEDSHIM_OK:
            return
        if self.playing:
            return  # already animating
        self.playing = True
        self._stop_event.clear()
        # create_daemon returns a started Thread; keep a reference for shutdown
        self._anim_thread = create_daemon(target=self._rainbow, args=(self._stop_event,))

    def _handle_playing_stopped(self, _):
        if not LEDSHIM_OK:
            return
        if not self.playing:
            return
        self.playing = False
        self._stop_event.set()
        # don't join daemon thread (by design); just stop the loop and clear LEDs
        try:
            ledshim.clear()
            ledshim.show()
        except Exception as e:
            LOG.debug(f"LED clear failed: {e}")

    # ---------- Animation ----------
    def _rainbow(self, stop_event: threading.Event):
        """Rainbow marquee while playing."""
        num = getattr(ledshim, "NUM_PIXELS", 28)
        spacing = 360.0 / max(1, num)
        # main loop
        while not stop_event.is_set() and self.playing:
            hue = int(time.time() * 100) % 360
            for x in range(num):
                h = ((hue + x * spacing) % 360) / 360.0
                r, g, b = [int(c * 255) for c in colorsys.hsv_to_rgb(h, 1.0, 1.0)]
                try:
                    ledshim.set_pixel(x, r, g, b)
                except Exception as e:
                    LOG.debug(f"set_pixel failed at {x}: {e}")
                    break
            try:
                ledshim.show()
            except Exception as e:
                LOG.debug(f"ledshim.show failed: {e}")
            time.sleep(0.02)  # ~50 FPS, low CPU

    # ---------- Lifecycle ----------
    def shutdown(self):
        # PHAL plugins can implement shutdown for clean exits
        try:
            self._handle_playing_stopped(None)
        finally:
            super().shutdown()

