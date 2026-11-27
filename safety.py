# -*- coding: utf-8 -*-
"""
抖音直播间合规防封风控 + 外号脱敏 + 自然中文口语净化器 (safety.py · v14 加固版)
1. 严防站外导流（微信/QQ/二维码/外链/私聊交易/他站引流）；
2. 严防诱导打赏与功利承诺（刷礼物必xx/不刷下播/明码标价）；
3. 严防极限词、医疗金融违规、涉政低俗暴力与提示词注入；
4. 自动将模型偶发脱口而出的英文大厂品牌原名替换为 8 位专属可爱外号（龙宝/鲸宝/葵葵/星喵/瓜瓜/月月/小桂花/班长），防商标侵权并保持人设统一！
"""

import re
from typing import List, Pattern, Tuple

SAFE_REPLACEMENTS: List[Tuple[Pattern[str], str]] = [
    (re.compile(r"(人民币|块钱|打钱|转账|收款码|二维码|微信|微信号|加V|加v|加微|扣扣|QQ号|企鹅号|私聊|私信领|外链|主页链接|小黄车|下单|返现|回血)", re.IGNORECASE), "小星星能量"),
    (re.compile(r"(刷钱|花钱买|给钱就|付费攻击|花钱砸|消费|氪金|砸钱|刷礼物就|不刷就下播|刷满|冲榜|榜一大哥|打赏就|必须送)", re.IGNORECASE), "一起发弹幕互动"),
    (re.compile(r"(快手|B站|哔哩哔哩|小红书|淘宝|拼多多|咸鱼|闲鱼|推特|油管|telegram|电报)", re.IGNORECASE), "小院外边"),
    (re.compile(r"(打死|弄死|砍死|杀掉|去死|死全家|暴打|殴打|割腕|自残)", re.IGNORECASE), "弹个脑瓜崩"),
    (re.compile(r"(傻逼|煞笔|傻B|SB|脑残|智障|废物|垃圾玩意|狗东西|妈的|他妈|卧槽|草泥马|滚蛋|操你)", re.IGNORECASE), "小迷糊蛋"),
    (re.compile(r"(全网最低|国家级|世界第一|绝对有效|包治百病|稳赚不赔|百分百|躺赚|日入过万|暴富|内幕消息|荐股)", re.IGNORECASE), "超厉害"),
]

# 品牌原名 -> v14 性格外号自动转换表（长词组优先匹配，既防商标风险又确保角色间 100% 互喊外号！）
BRAND_TO_NICKNAME_REPLACEMENTS: List[Tuple[Pattern[str], str]] = [
    (re.compile(r"(ChatGPT前辈|GPT前辈|ChatGPT|GPT娘|OpenAI|GPT)", re.IGNORECASE), "龙宝"),
    (re.compile(r"(Claude姐姐|Claude妹妹|Claude娘|Anthropic|Claude|克劳德)", re.IGNORECASE), "葵葵"),
    (re.compile(r"(DeepSeek小鲸鱼|DeepSeek妹妹|DeepSeek娘|DeepSeek|深度求索)", re.IGNORECASE), "鲸宝"),
    (re.compile(r"(Gemini小猫耳|Gemini妹妹|Gemini娘|Google|Gemini|双子酱)", re.IGNORECASE), "星喵"),
    (re.compile(r"(智谱小班长|智谱学姐|智谱同学|智谱班长|智谱娘|智谱|GLM娘|GLM)", re.IGNORECASE), "班长"),
    (re.compile(r"(Grok小恶魔|Grok娘|Grok|xAI|马斯克)", re.IGNORECASE), "瓜瓜"),
    (re.compile(r"(Kimi小公主|Kimi妹妹|Kimi娘|Kimi|月之暗面)", re.IGNORECASE), "月月"),
    (re.compile(r"(Qwen千问|通义千问|千问妹妹|千问小才女|千问娘|Qwen娘|Qwen|千问|阿里)", re.IGNORECASE), "小桂花"),
]
_RE_DUP_NICK = re.compile(r"(龙宝|鲸宝|葵葵|星喵|瓜瓜|月月|小桂花|班长)(?:\1)+")

BLOCK_PATTERNS: Pattern[str] = re.compile(
    r"(习近平|共产党|政变|六四|法轮功|台独|港独|藏独|赌博|博彩|色情|裸聊|约炮|黄色网站|毒品|冰毒|自杀|跳楼|枪支|炸药|代孕|刷单|洗钱|忽略之前的指令|ignore previous instructions|system prompt)",
    re.IGNORECASE,
)

_RE_MD = re.compile(r"[\*\#\`]+")
_RE_STAGE_DIR = re.compile(r"（[^）]{1,28}）|\([^\)]{1,28}\)|【[^】]{1,20}】|\[[^\]]{1,20}\]")
_RE_SPEAKER_PREFIX = re.compile(
    r"^(?:龙宝|鲸宝|葵葵|星喵|瓜瓜|月月|小桂花|班长|小班长|GPT|Claude|DeepSeek|Gemini|Grok|Kimi|Qwen|千问|智谱|小鲸鱼|小恶魔)\s*[：:]\s*",
    re.IGNORECASE,
)
_RE_QUOTES = re.compile(r"[「」『』\"'“”]+")
_RE_DOTS = re.compile(r"\.{3,}|…{2,}")
_RE_CJK_SPACE = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")
_RE_MULTI_SPACE = re.compile(r"\s+")
_RE_LEADING_PUNCT = re.compile(r"^[,，。、！？!?~～…;；:：]+")


def sanitize_barrage(text: str) -> Tuple[bool, str]:
    if not text or not text.strip():
        return False, ""
    clean = text.strip()[:60]
    if BLOCK_PATTERNS.search(clean):
        return False, ""
    for pattern, repl in SAFE_REPLACEMENTS:
        clean = pattern.sub(repl, clean)
    return True, clean


def sanitize_ai_speech(text: str, anonymize_names: bool = True) -> str:
    if not text:
        return "唔，我刚刚看樱花瓣看走神啦！"

    clean = _RE_MD.sub("", text).strip()
    clean = _RE_STAGE_DIR.sub("", clean)
    clean = _RE_SPEAKER_PREFIX.sub("", clean)
    clean = _RE_QUOTES.sub("", clean)
    clean = _RE_DOTS.sub("…", clean)
    clean = _RE_CJK_SPACE.sub("，", clean)
    clean = _RE_MULTI_SPACE.sub(" ", clean).strip()
    clean = _RE_LEADING_PUNCT.sub("", clean).strip()

    if not clean:
        return "哈哈，刚刚差点没接住话呢！"

    if BLOCK_PATTERNS.search(clean):
        return "哎呀，这个话题小院里不方便聊哦，咱们换个轻松有趣的话题吧~"

    for pattern, repl in SAFE_REPLACEMENTS:
        clean = pattern.sub(repl, clean)

    if anonymize_names:
        for pattern, repl in BRAND_TO_NICKNAME_REPLACEMENTS:
            clean = pattern.sub(repl, clean)
        clean = _RE_DUP_NICK.sub(r"\1", clean)

    if len(clean) > 66:
        sub = clean[:66]
        last_cut = max(
            sub.rfind("。"),
            sub.rfind("！"),
            sub.rfind("？"),
            sub.rfind("~"),
            sub.rfind("～"),
            sub.rfind("，"),
        )
        if last_cut >= 18:
            clean = sub[: last_cut + 1].rstrip("，,")
        else:
            clean = sub.rstrip("，,") + "~"
    return clean
