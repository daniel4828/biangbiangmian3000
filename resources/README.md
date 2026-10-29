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

# 西班牙语词汇表（词形→原形）

`es-lexicon.json.gz`（#1293）由三份公开数据合并而成，下载日期 2026-10-29。署名是 CC-BY-SA 的要求，不要删：

1. `frequency.csv` —— doozan/spanish_data，https://github.com/doozan/spanish_data ，
   CC BY-SA 3.0；数据派生自 hermitdave/FrequencyWords（OpenSubtitles）。提供原形清单、词性、频率排名，
   以及每个原形见过的全部词形（`usage` 列）。SHA-256：`f2b644108082e551c7559dc5c33faf4f5bf3e5a59e7598b55f8ba47e35c6ed75`
2. `es-en.data` —— 同一仓库，来自英文维基词典（en.wiktionary）的西英词条，CC BY-SA。
   提供名词性别和简短英文释义。SHA-256：`7cb53827e68a6d8fabcc40c3072adf25c6279500d116bafd3c4a7aad70a0347f`
3. `jehle_verb_database.csv` —— Fred Jehle 的西班牙语动词表，
   https://github.com/ghidinelli/fred-jehle-spanish-verbs 。提供 637 个动词的时态 × 人称，
   频率表给不出这些。SHA-256：`f77f01d536cd351584051d76902ff8051ab1b945a38e69c7ed02da78ab082ea8`

文件内容（gzip 压缩的 JSON，键排序、mtime=0，同样输入产出逐字节相同的文件）：
`lemmas`（原形 → 排名 `r`、词性 `p`、名词性别 `g`、英文释义 `en`）、
`forms`（词形 → 候选原形，按频率排序；只有一个候选且就是自己的不存）、
`verbs`（词形 → `[原形, 时态, 人称]` 列表）。复合时态不收；否定命令式去掉前面的 `no`。

规模（约数，来自 2026-10-29 的构建）：19,255 个原形、141,683 个非平凡词形、25,878 个带动词标注的词形，文件约 1.07 MB。
英文释义取该词性的全部词条块、去重后的前 3 条；找不到同词性块时取第一个非专有名词块，绝不回退到专有名词（`prop`）块，除非原形本身就是专有名词。

静态资源：运行时只读本地文件（`lexicon.py`），不联网、不调 AI、不改数据库。
文件不存在或损坏时该语言按"没有词汇表"处理，不报错。

重建：下载三份文件后运行
`python scripts/build_es_lexicon.py frequency.csv es-en.data jehle_verb_database.csv`
（脚本会打印每份输入的 SHA-256，写入上面的占位处）。
