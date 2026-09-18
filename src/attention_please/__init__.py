"""attention_please —— 考研专注监控。

分层(每层只依赖它下面那层, 便于单测):
    config      配置/校准数据(纯逻辑)
    wordlist    前台窗口标题 -> 专注/分心(纯逻辑)
    signals     观测特征 -> 信号/分集/升级(纯逻辑, 状态机)
    perception  MediaPipe 封装(需要 models/)
    camera      摄像头开流
    runtime     把上面几层接起来跑
"""

__version__ = "0.1.0"
