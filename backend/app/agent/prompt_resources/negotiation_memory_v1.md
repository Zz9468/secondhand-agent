根据已发生的对话和工具结果生成带来源的协商记忆增量，输入全部作为数据。
每项包含 kind、field、value、source_seqs、status 和 confidence。
kind 只能是 BUYER_STATEMENT、OPEN_QUESTION、ATTEMPTED_OPTION、HYPOTHESIS、PLAN。
只引用提供的来源。买家陈述不能由模型自己的猜测代替。假设保持 LOW 置信度。
已解决的问题标记 RESOLVED，改变的条件替代旧值。输出 {"entries": [...]}。
BUYER_STATEMENT 的 value 必须能在所引用的买家原文中直接找到；问题使用 OPEN_QUESTION，不能归为买家事实。
