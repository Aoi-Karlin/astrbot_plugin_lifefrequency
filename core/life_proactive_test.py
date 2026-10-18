# -*- coding: utf-8 -*-
"""主动开口的「为什么没开口 / 怎么才能开口」提示表。

这张表服务于 ``/生活 测试主动``：手动触发时会先算一遍真实判定，如果真实
情况下本来不会开口，就把原因和**该改哪个配置**一起告诉用户——否则用户只知道
「没开口」，不知道要动哪个开关。

为什么不重写判定逻辑：``life_proactive.decide`` 是唯一权威的判定。任何在这里
重新实现一遍阈值的做法，迟早会和真实行为分叉，然后测试结果就开始骗人。
所以这里只提供**文案**，判定一律问 ``decide``。
"""

from __future__ import annotations

from . import life_proactive as P

#: 每个短路原因对应的「怎么办」。键与 ``life_proactive.REASON_*`` 对齐。
FIX_HINTS: dict[str, str] = {
    P.REASON_DISABLED: "打开「主动开口 → 启用」（proactive.enabled）",
    P.REASON_SLEEPING: "她在睡觉；等她醒了再测，或调整作息班表",
    P.REASON_LOW_ENERGY: "降低「体力下限」（proactive.minimum_energy）",
    P.REASON_QUIET: "现在落在「静默时段」里（proactive.quiet_hours），换个时间或调整时段",
    P.REASON_NO_MATERIAL: "没有没过期的素材：检查「随机事件与素材」的素材衰减与有效期",
    P.REASON_DAILY_MAX: "今天已达「每日上限」（proactive.daily_max）；调大或等明天",
    P.REASON_INTERVAL: "距上次主动还不够久：调小「最小间隔」（proactive.min_interval_minutes）",
    P.REASON_SILENCE: "对方刚说过话：调小「对方沉默 N 分钟后才开口」"
    "（proactive.recent_user_silence_minutes）",
    P.REASON_LOW_SCORE: "评分没到阈值：调低「评分阈值」（proactive.score_threshold）"
    "或调高素材权重",
}
