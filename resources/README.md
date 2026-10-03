# 中文整词词频

`subtlex-ch-ranks.json` 来源：Cai, Q., & Brysbaert, M. (2010).
SUBTLEX-CH: Chinese Word and Character Frequencies Based on Film Subtitles.
PLOS ONE, 5(6), e10729. https://doi.org/10.1371/journal.pone.0010729

官方页面：https://www.ugent.be/pp/experimentele-psychologie/en/research/documents/subtlexch
下载：https://www.ugent.be/pp/experimentele-psychologie/en/research/documents/subtlexch/subtlexchwf.zip
下载日期：2026-10-03。原始 SUBTLEX-CH-WF 文件 SHA-256：
`086536450b1f77d0c7ff3ac0fc8375897162ace807d3167bec48b4c493434077`

包含 99,121 个整词；按 WCount 降序，同频词按词形排序，排名从 1 开始。
数据来自 33,546,516 词的电影字幕语料；这是该语料中的频率，不代表所有语境下的重要性。
匹配采用原词形，不拆字、不推测短语频率。未收录及非中文词返回 null。
运行时读取本地文件，不联网、不修改用户数据库。

重建：下载官方 ZIP 后运行 `python scripts/build_word_frequency.py /path/to/subtlexchwf.zip`。
