"""在一份合成语料上跑真实的检索评测，并把验收门槛写成断言。

plan 的指标里有 Recall@10 ≥ 0.85。在那之前它只是一个数字；写成断言之后，它变成一条会
失败的测试——**检索质量下降会和代码缺陷一样被挡住**，而不是等到交付前才发现。

为什么是合成语料：这个产品没有真实用户数据，而"等有数据了再评测"等于永远不评测。合成的
语料是**受控的**——我们知道每一问的答案在哪一页，所以分数可解释；它当然不等于真实教材的
难度，这一条写在下面的已知限制里，而不是假装它代表真实表现。

语料刻意包含两类问题：

* **用词与原文重合的**（"判别式决定了什么"）——词法那一路应该能拿到；
* **用词与原文不同的**（"怎么把一般式变成顶点式" 对原文"配方法把一般式写成顶点式"）——
  这一路靠的是字符二元组的覆盖，也是真正能看出分词选择好坏的地方。
"""

from __future__ import annotations

import pytest

from backend.knowe_rag import ParsedBlock, build_chunks
from backend.knowe_rag.evaluation import EvaluationCase, evaluate_retrieval
from backend.knowe_rag.retrieval import ScopeIndex

#: 一份十页的合成教材。每一页一句，句子之间刻意共用一些词（"抛物线"、"二次"、"交点"），
#: 因为真实的教材就是这样的——如果每一页用完全不同的词，评测会虚高。
CORPUS: tuple[str, ...] = (
    "二次函数的一般形式是 y 等于 a x 平方加 b x 加 c，其中 a 不等于零。",
    "抛物线的开口方向由二次项系数 a 决定：a 大于零时开口向上，a 小于零时开口向下。",
    "抛物线的对称轴是直线 x 等于负 b 除以二 a，顶点在对称轴上。",
    "判别式 delta 等于 b 平方减四 a c，它决定抛物线与横轴的交点个数。",
    "当判别式大于零时有两个交点，等于零时有一个交点，小于零时没有交点。",
    "求根公式是 x 等于负 b 加减根号 delta 再除以二 a，它由配方法推导而来。",
    "配方法把一般式写成顶点式，顶点坐标就是括号里那个常数。",
    "函数图像的平移遵循左加右减、上加下减的规律。",
    "一元二次方程的解就是对应抛物线与横轴交点的横坐标。",
    "实际问题中，二次函数的最大值或最小值出现在顶点处。",
)

#: 问题与"要答对它至少得召回哪一页"。标注用的是页号——用户看得到、也写得进标注；
#: 评测在比对前会把检索返回的 child 位置归一成页。
CASES: tuple[EvaluationCase, ...] = (
    EvaluationCase(
        question="二次函数的一般形式是什么",
        expected_locations=frozenset({"page:1"}),
    ),
    EvaluationCase(
        question="抛物线的开口方向由什么决定",
        expected_locations=frozenset({"page:2"}),
    ),
    EvaluationCase(
        question="对称轴的公式是什么",
        expected_locations=frozenset({"page:3"}),
    ),
    EvaluationCase(
        question="判别式决定了什么",
        expected_locations=frozenset({"page:4"}),
    ),
    EvaluationCase(
        question="什么情况下抛物线与横轴没有交点",
        expected_locations=frozenset({"page:5"}),
    ),
    EvaluationCase(
        question="求根公式是怎么推导出来的",
        expected_locations=frozenset({"page:6"}),
    ),
    EvaluationCase(
        question="怎么把一般式变成顶点式",
        expected_locations=frozenset({"page:7"}),
    ),
    EvaluationCase(
        question="图像平移有什么规律",
        expected_locations=frozenset({"page:8"}),
    ),
    EvaluationCase(
        question="一元二次方程的解在图像上是什么",
        expected_locations=frozenset({"page:9"}),
    ),
    EvaluationCase(
        question="实际问题里的最大值在哪里取到",
        expected_locations=frozenset({"page:10"}),
    ),
    # ── 下面这三问是**刻意加难的** ──────────────────────────────────
    #
    # 上面十问的用词与原文高度重合，第一版跑出来是满分 1.00——而一个永远满分的评测不会在
    # 检索退化时报警，它只是在耗时间。这三问各自针对一种真实问法：
    #
    # 同义改写（"朝哪边开口" 对原文 "开口方向"）、术语别名（"横坐标" 对 "x 等于"）、
    # 以及跨页推理（"没有实数根" 要把判别式那页和交点那页连起来）。
    EvaluationCase(
        question="抛物线朝哪边开口",
        expected_locations=frozenset({"page:2"}),
    ),
    EvaluationCase(
        question="顶点的横坐标怎么算",
        expected_locations=frozenset({"page:3"}),
    ),
    EvaluationCase(
        question="方程没有实数根的时候图像长什么样",
        expected_locations=frozenset({"page:5"}),
    ),
)

#: ⚠ **语料必须远大于 k，否则 Recall@k 恒为 1。** 第一版这份语料只有十页而 k=10：检索引擎
#: 返回全部十块，于是每一问都"命中"，分数永远是满分——一个恒真的评测比没有评测更糟，因为它
#: 看起来像达标了。下面这三十页干扰项就是为了让分数真的能变动。它们刻意来自别的学科，与
#: 数学部分的词汇不重叠。
DISTRACTORS: tuple[str, ...] = (
    "光合作用是绿色植物利用光能把二氧化碳和水合成有机物的过程。",
    "细胞膜控制物质进出细胞，具有选择透过性。",
    "地壳中含量最多的元素是氧，其次是硅。",
    "地球自转产生昼夜交替，公转产生四季变化。",
    "鸦片战争是中国近代史的开端。",
    "丝绸之路连接了中原与西域的商贸往来。",
    "英语一般现在时表示经常发生的动作或状态。",
    "现在完成时强调过去发生的动作对现在造成的影响。",
    "声音在空气中的传播速度约为每秒三百四十米。",
    "光的反射定律指出入射角等于反射角。",
    "元素周期表按原子序数递增排列。",
    "酸与碱反应生成盐和水，这类反应叫做中和反应。",
    "人体呼吸系统由鼻腔、咽、喉、气管和肺组成。",
    "血液循环分为体循环和肺循环两条途径。",
    "中国的季风气候夏季高温多雨，冬季寒冷干燥。",
    "板块构造学说认为岩石圈由若干板块拼合而成。",
    "计算机的中央处理器负责运算和控制。",
    "二进制只用零和一两个数字表示数值。",
    "算法的复杂度描述运行时间随输入规模的增长。",
    "数据库的索引可以加快查询但会拖慢写入。",
    "文艺复兴的核心思想是人文主义。",
    "工业革命首先发生在十八世纪的英国。",
    "《论语》记录了孔子及其弟子的言行。",
    "唐诗分为古体诗和近体诗两大类。",
    "机械能守恒的条件是只有重力或弹力做功。",
    "牛顿第一定律说明物体不受力时保持原有运动状态。",
    "溶液的质量分数等于溶质质量除以溶液质量。",
    "摩尔是物质的量的单位，一摩尔约含六点零二乘十的二十三次方个微粒。",
    "基因是控制生物性状的遗传物质的功能单位。",
    "生态系统中的能量沿食物链逐级递减。",
)

ACCEPTANCE_RECALL_AT_10 = 0.85


def _corpus_index() -> ScopeIndex:
    """数学那十页 + 干扰项，全部进同一个索引。

    干扰项是这份评测能成立的前提：没有它们，十块语料配上 k=10 会让每一问都"命中"。
    """
    pages = list(CORPUS) + list(DISTRACTORS)
    index = ScopeIndex()
    for chunk in build_chunks("textbook", tuple(
        ParsedBlock(text=text, location=f"page:{number}", kind="page")
        for number, text in enumerate(pages, start=1)
    )):
        index.add(chunk)
    return index


def test_the_corpus_meets_the_acceptance_recall_threshold() -> None:
    """plan 的 Recall@10 ≥ 0.85。

    失败时把完全没召回的问题一起打出来：只看一个平均分，没法知道是"某一类问题系统性召不回"
    还是"个别问题标注写偏了"——而这两件事要做的处理完全不同。
    """
    report = evaluate_retrieval(_corpus_index(), CASES, k=10)

    assert report.mean_recall >= ACCEPTANCE_RECALL_AT_10, (
        f"Recall@10 = {report.mean_recall:.2f}，低于门槛 {ACCEPTANCE_RECALL_AT_10}；"
        f"完全没召回的问题：{[outcome.question for outcome in report.worst]}"
    )


def test_no_question_comes_back_completely_empty() -> None:
    """任何一问都不该一条都召不回。

    平均分达标可以掩盖"有两问是零分"，而零分意味着用户问那句话时，系统会说"没有依据"——
    对一个只会词法匹配的检索器，这通常说明某个常用说法压根没被分词覆盖。
    """
    report = evaluate_retrieval(_corpus_index(), CASES, k=10)

    assert report.worst == (), [outcome.question for outcome in report.worst]


def test_the_report_is_specific_enough_to_act_on() -> None:
    """每一问都要能看到"期望哪一页、来了哪些页"。

    一个只有分数的报告没法用来改东西——要复现就得自己去翻检索结果，而省掉那一步正是做评测
    的意义。
    """
    report = evaluate_retrieval(_corpus_index(), CASES, k=10)

    assert len(report.outcomes) == len(CASES)
    for outcome in report.outcomes:
        assert outcome.expected
        assert outcome.retrieved_locations
        assert outcome.matched or outcome.missed


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.question)
def test_every_case_is_answered_from_the_page_its_annotation_names(
    case: EvaluationCase,
) -> None:
    """逐问检查，失败信息直接是那句问题。

    整份评测失败时给出的是平均分，而平均分不指向任何一行代码；逐问参数化之后，失败列表本身
    就是待办清单。
    """
    report = evaluate_retrieval(_corpus_index(), [case], k=10)

    assert report.outcomes[0].recall == 1.0, (
        f"{case.question!r} 期望 {sorted(case.expected_locations)}，"
        f"实际召回 {list(report.outcomes[0].retrieved_locations)}"
    )


def test_the_corpus_is_much_larger_than_k() -> None:
    """语料必须**远大于** k，否则 Recall@k 恒为 1。

    第一版这份语料只有十页而 k=10：检索引擎返回全部十块，于是每一问都"命中"，分数永远是
    满分。一个恒真的评测比没有评测更糟——它看起来像达标了。

    这条测试把那个前提钉在这里：谁把语料改小到 k 的量级，它会立刻失败。
    """
    assert len(DISTRACTORS) >= 20, len(DISTRACTORS)
    assert len(CORPUS) + len(DISTRACTORS) >= 4 * 10, len(CORPUS) + len(DISTRACTORS)


def test_the_distractors_really_are_distractors() -> None:
    """干扰项里不能混进数学词汇。

    混进去的话，它们会跟正确答案抢排名——那测的就不是"检索能不能区分学科"，而是"语料写得
    巧不巧"。这一条检查的是语料本身，不是检索。
    """
    mathematics = {"抛物线", "判别式", "对称轴", "顶点", "二次函数", "交点"}

    for page in DISTRACTORS:
        overlap = {word for word in mathematics if word in page}
        assert not overlap, (page, overlap)


def test_the_score_actually_moves_when_k_shrinks() -> None:
    """k 从 10 降到 1，分数必须变——否则这份语料测不出**排名**质量。

    一个把正确答案排在第 8 名的检索器在 Recall@10 下是满分的，在 Recall@1 下不是。如果两者
    给出同一个分数，说明正确答案永远排在第一位，那么这份语料的满分只说明"东西找得到"，不
    说明"它找得准"。

    这是对**评测装置**的检查，不是对检索的检查——一个恒真的评测比没有评测更糟。
    """
    index = _corpus_index()

    at_ten = evaluate_retrieval(index, CASES, k=10)
    at_one = evaluate_retrieval(index, CASES, k=1)

    assert at_ten.mean_recall == 1.0, "前提：k=10 时应当全对"
    assert at_one.mean_recall < at_ten.mean_recall, (
        f"k=1 时也是满分 {at_one.mean_recall}——这份语料区分不出排名质量，"
        "需要加更难的用例或更多干扰项"
    )


def test_the_known_limitation_of_this_corpus_is_written_down() -> None:
    """把这份评测**测不到什么**写下来，免得它的分数被当成真实表现。

    合成语料只有十页、每页一句、没有表格与公式排版，也没有"同一段话在多页上重复"这类真实
    情况。它验证的是分词与融合在受控输入上是否工作，不是"在真实教材上能不能到 0.85"。
    """
    assert len(CORPUS) == 10
    assert all(len(page) < 100 for page in CORPUS), "合成语料不该偷偷变复杂"
    # 每一页至少被一问覆盖到，否则有一页从没被检查过。
    covered = {location for case in CASES for location in case.expected_locations}
    assert covered == {f"page:{number}" for number in range(1, 11)}
