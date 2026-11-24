from .npu_collector import MONITOR, format_stats


class NPUMonitorStats:
    """Outputs the current NPU / RAM usage as text (also prints to the console)."""

    CATEGORY = "utils/NPU Monitor"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("stats",)
    FUNCTION = "run"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            "optional": {
                # 接任意输入，用来把它串在某个节点之后，在那一步执行完时采样
                "any_input": ("*", {}),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, input_types=None, **kwargs):
        return True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")  # always re-run

    def run(self, any_input=None):
        text = format_stats(MONITOR.sample_now())
        print("[NPU Monitor]\n" + text)
        return {"ui": {"text": [text]}, "result": (text,)}


NODE_CLASS_MAPPINGS = {"NPUMonitorStats": NPUMonitorStats}
NODE_DISPLAY_NAME_MAPPINGS = {"NPUMonitorStats": "NPU Monitor (Stats)"}
