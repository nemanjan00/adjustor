import logging
import time
from typing import cast

from hhd.plugins import Context, HHDPlugin, HHDSettings, load_relative_yaml
from hhd.plugins.conf import Config

from adjustor.core.lenovo import (
    MIN_CURVE,
    TdpMode,
    get_fan_curve,
    get_fast_tdp,
    get_full_fan_speed,
    get_power_light,
    get_steady_tdp,
    get_tdp_mode,
    set_fan_curve,
    set_fast_tdp,
    set_full_fan_speed,
    set_power_light,
    set_slow_tdp,
    set_steady_tdp,
    set_tdp_mode,
)

logger = logging.getLogger(__name__)

APPLY_DELAY = 0.5

class LenovoDriverPlugin(HHDPlugin):
    def __init__(self) -> None:
        self.name = f"adjustor_lenovo"
        self.priority = 6
        self.log = "adjl"
        self.enabled = False
        self.initialized = False
        self.enforce_limits = True
        self.startup = True
        self.old_conf = None
        self.fan_curve_set = False

        self.queue_fan = None
        self.queue_tdp = None

    def settings(self):
        if not self.enabled:
            self.initialized = False
            self.old_conf = None
            self.startup = True
            return {}
        
        self.initialized = True
        out = {"tdp": {"lenovo": load_relative_yaml("settings.yml")}}
        if not self.enforce_limits:
            out["tdp"]["lenovo"]["children"]["tdp"]["modes"]["custom"]["children"][
                "tdp"
            ]["max"] = 40
        return out

    def open(
        self,
        emit,
        context: Context,
    ):
        pass

    def update(self, conf: Config):
        self.enabled = conf["tdp.general.enable"].to(bool)
        self.enforce_limits = conf["tdp.general.enforce_limits"].to(bool)
        if not self.enabled or not self.initialized:
            self.old_conf = None
            self.startup = True
            self.fan_curve_set = False
            return
        
        #
        # Checks
        #

        # Initialize values so we do not query them all the time
        if self.startup:
            conf["tdp.lenovo.ffss"] = get_full_fan_speed()
            conf["tdp.lenovo.power_light"] = get_power_light()
            self.startup = False

        # If not old config, exit, as values can not be set
        if not self.old_conf:
            self.old_conf = conf["tdp.lenovo"]
            return

        curr = time.time()

        #
        # Other options
        #
        ffss = conf["tdp.lenovo.ffss"].to(bool)
        if ffss is not None and ffss != self.old_conf["ffss"].to(bool):
            set_full_fan_speed(ffss)

        power_light = conf["tdp.lenovo.power_light"].to(bool)
        if power_light is not None and power_light != self.old_conf[
            "power_light"
        ].to(bool):
            set_power_light(power_light)
        
        #
        # TDP
        #
        
        # Update tdp mode if user changed through the app
        tdp_reset = False
        mode = conf["tdp.lenovo.tdp.mode"].to(str)
        if mode is not None and mode != self.old_conf["tdp.mode"].to(str):
            set_tdp_mode(cast(TdpMode, mode))
            tdp_reset = True
        
        # Grab from power button
        new_mode = get_tdp_mode()
        if new_mode != mode:
            tdp_reset = True
        conf["tdp.lenovo.tdp.mode"] = new_mode

        # Reset fan curve on mode change
        # Has to happen before setting the stdp, ftdp values, in case
        # we are in custom mode
        fan_mode = conf["tdp.lenovo.fan.mode"].to(str)
        if (
            fan_mode != self.old_conf["fan.mode"].to(str) and fan_mode != "manual"
        ):
            tdp_mode = get_tdp_mode()
            if tdp_mode:
                set_tdp_mode("performance")
                set_tdp_mode(tdp_mode)
                tdp_reset = True

        # In custom mode, re-apply settings with debounce
        if new_mode == "custom":
            # Check user changed values
            steady = conf["tdp.lenovo.tdp.custom.tdp"].to(int)
            steady_updated = steady and steady != self.old_conf[
                "tdp.custom.tdp"
            ].to(int)
            boost = conf["tdp.lenovo.tdp.custom.boost"].to(bool)
            boost_updated = boost != self.old_conf["tdp.custom.boost"].to(bool)
            
            # If yes, queue an update
            # Debounce
            if steady_updated or boost_updated:
                self.queue_tdp = curr + APPLY_DELAY
            
            if (self.queue_tdp and self.queue_tdp < curr) or tdp_reset:
                self.queue_tdp = None
                if boost:
                    set_steady_tdp(steady)
                    set_slow_tdp(steady + 2)
                    set_fast_tdp(min(42, int(steady * 41 / 30)))
                else:
                    set_steady_tdp(steady)
                    set_slow_tdp(steady)
                    set_fast_tdp(steady)

        # Fan curve stuff
        # If tdp reset, so was the curve
        if tdp_reset:
            self.fan_curve_set = False

        # Handle fan curve resets
        if conf["tdp.lenovo.fan.manual.reset"].to(bool):
            conf["tdp.lenovo.fan.manual.reset"] = False
            for i, v in enumerate(MIN_CURVE):
                conf[f"tdp.lenovo.fan.manual.st{(i + 1)*10}"] = v
        
        # Handle fan curve limits
        if conf["tdp.lenovo.fan.manual.enforce_limits"].to(bool):
            for i, v in enumerate(MIN_CURVE):
                if conf[f"tdp.lenovo.fan.manual.st{(i + 1)*10}"].to(int) < v:
                    conf[f"tdp.lenovo.fan.manual.st{(i + 1)*10}"] = v

        # Check if fan curve has changed
        # Use debounce logic on these changes
        for i in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100):
            if conf[f"tdp.lenovo.fan.manual.st{i}"].to(int) != self.old_conf[
                f"fan.manual.st{i}"
            ].to(int):
                self.queue_fan = curr + APPLY_DELAY

        apply_curve = (self.queue_fan and self.queue_fan < curr) or not self.fan_curve_set
        if conf["tdp.lenovo.fan.mode"].to(str) == "manual" and apply_curve:
            try:
                set_fan_curve(
                    [
                        conf[f"tdp.lenovo.fan.manual.st{i}"].to(int)
                        for i in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)
                    ]
                )
            except Exception as e:
                logger.error(f"Could not set fan curve. Error:\n{e}")
            self.fan_curve_set = True
            self.queue_fan = None
        
        # Save current config
        self.old_conf = conf["tdp.lenovo"]

    def close(self):
        pass
