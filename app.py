import streamlit as st
import pandas as pd
import pdfplumber
import json
from openai import OpenAI
import re
from docx import Document
from docx.shared import Pt
import io
import time  # 必须导入
# ... 其他导入保持不变 ...

# --- 新增：带指数退避的重试函数 ---
def call_ai_with_retry(client, model, messages, max_retries=3, delay=2):
    """
    遇到频率限制时自动重试：2s -> 4s -> 8s
    """
    for i in range(max_retries):
        try:
            return client.chat.completions.create(model=model, messages=messages)
        except Exception as e:
            # 如果是频率限制错误且还有重试机会
            if ("429" in str(e) or "rate_limit" in str(e).lower()) and i < max_retries - 1:
                wait_time = delay * (2 ** i)
                time.sleep(wait_time)
                continue
            raise e # 其他错误或重试耗尽则抛出

# 将结果导出到word中
def export_to_word(summary, analysis, refined_data):
    doc = Document()

    # 设置全局字体（可选）
    style = doc.styles['Normal']
    style.font.name = '微软雅黑'
    style.font.size = Pt(10)

    # 1. 标题
    doc.add_heading('简历深度优化报告', 0)

    # 2. 整体求职策略
    doc.add_heading('一、首席人才官：整体求职策略建议', level=1)
    doc.add_paragraph(summary)

    # 3. 岗位胜任力解析
    doc.add_heading('二、岗位胜任力深度画像', level=1)
    doc.add_paragraph(analysis)

    # 4. 各模块精修建议
    doc.add_heading('三、简历各模块精修建议', level=1)

    for section_name, content in refined_data.items():
        doc.add_heading(f'模块：{section_name}', level=2)
        # AI 返回的是 Markdown 格式，这里简单处理：
        # 如果你想要更完美的表格导出，需要解析 Markdown 表格，
        # 这里先以文本流形式导出，保证内容完整和分段清晰。
        doc.add_paragraph(content)
        doc.add_page_break()  # 每个大模块换一页，保持整洁

    # 将文档保存到内存流
    bio = io.BytesIO()
    doc.save(bio)
    return bio.getvalue()

def split_resume_by_sections(text):
    """
    使用正则表达式将简历文本切分为模块
    """
    # 定义常见的简历标题关键词
    patterns = {
        "工作经历": r"(工作经历|实习|实习经历|Experience|Work)",
        "项目经历": r"(项目经历|项目经验|个人项目|Projects)",
        "技能证书": r"(专业技能|技能特长|语言能力|证书|Skills)",
        "自我评价": r"(自我评价|个人总结|About Me)"
    }

    # 1. 寻找所有可能的切分点
    matches = []
    for section, pattern in patterns.items():
        # 使用 re.MULTILINE 尝试匹配行首，减少正文干扰
        for match in re.finditer(pattern, text, re.IGNORECASE):
            matches.append((match.start(), section))

    # 2. 按文本位置排序
    matches.sort()

    # 3. 提取各个模块
    sections = {}

    # 提取“基本信息”（第一个标题之前的内容）
    if matches and matches[0][0] > 0:
        sections["基本信息"] = text[:matches[0][0]].strip()

    for i in range(len(matches)):
        start_pos, section_name = matches[i]
        # 结尾是下一个匹配点的开始，或者是全文结尾
        end_pos = matches[i + 1][0] if i + 1 < len(matches) else len(text)

        content = text[start_pos:end_pos].strip()

        # 避免内容重复：如果一个标题被匹配了多次，合并它们
        if section_name in sections:
            sections[section_name] += "\n" + content
        else:
            sections[section_name] = content

    # 4. 兜底处理：如果没有匹配到任何标题
    if not sections:
        return {"完整简历": text}

    return sections

# --- 1. 页面基本配置 ---
st.set_page_config(page_title="暑期实习求职利器", layout="wide", initial_sidebar_state="expanded")
st.title("🚀 暑期实习岗位精准匹配与优化工具")

# --- 2. 隐私保护声明 ---
with st.expander("🛡️ 隐私保护与数据安全说明", expanded=False):
    st.info("""
    **本工具郑重承诺：**
    1. **不留痕迹**：你上传的简历和岗位表仅在服务器内存中实时处理。
    2. **不存储文件**：一旦你刷新或关闭网页，所有数据将自动彻底销毁。
    3. **脱敏建议**：你可以删除简历中的手机号、住址等敏感信息，不影响 AI 评估。
    """)

# --- 3. 侧边栏：授权与配置 ---
with st.sidebar:
    st.header("🔑 访问授权")
    # 优先从 Secrets 读取授权码，如果没有则需要手动输入（本地测试用）
    try:
        correct_auth_code = st.secrets["MY_AUTH_CODE"]
    except:
        correct_auth_code = "2024intern"  # 本地测试默认码

    user_code = st.text_input("请输入授权码", type="password")

    if user_code != correct_auth_code:
        st.warning("请输入正确的授权码以解锁功能。")
        st.stop()

    st.success("授权成功！")
    st.divider()
    st.header("⚙️ AI 配置")
    api_key = st.text_input("请输入你的 DeepSeek API Key", type="password", help="在此填入你的 API Key 即可开始使用")

    if not api_key:
        st.info("待输入 API Key...")
        st.stop()

# --- 侧边栏：优化偏好设置 ---
with st.sidebar:
    st.divider()
    st.header("🎨 简历定制偏好")
    opt_style = st.radio(
        "文风倾向",
        ["稳重务实型 (金融/医疗/传统行业)", "极简干练型 (互联网/咨询/大厂)", "充满活力型 (初创/创意/快消)"],
        index=1
    )
    detail_depth = st.select_slider(
        "细节挖掘深度",
        options=["点到为止", "标准修饰", "深度重构"],
        value="标准修饰"
    )


# 初始化 AI 客户端
client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

# --- 4. 功能一：精准匹配 (含硬筛选与全字段推送) ---
st.header("📅 第一步：岗位匹配")

col_up1, col_up2 = st.columns(2)
with col_up1:
    job_file = st.file_uploader("1. 上传岗位 Excel 表", type=["xlsx"])
with col_up2:
    cv_file = st.file_uploader("2. 上传你的简历 (PDF)", type=["pdf"])

if job_file and cv_file:
    # 加载数据
    df = pd.read_excel(job_file)

    # 筛选 UI 界面
    st.subheader("🔍 岗位精准筛选")
    c1, c2, c3 = st.columns(3)

    with c1:
        # 对应表头：工作地点
        city_list = df['工作地点'].dropna().unique().tolist() if '工作地点' in df.columns else []
        sel_cities = st.multiselect("实习区域 (工作地点)", options=city_list)

    with c2:
        # 对应表头：实习月数
        month_list = df['实习月数'].dropna().unique().tolist() if '实习月数' in df.columns else []
        sel_months = st.multiselect("实习时长 (月数)", options=month_list)

    with c3:
        # 对应表头：转正机会
        convert_list = df['转正机会'].dropna().unique().tolist() if '转正机会' in df.columns else []
        sel_convert = st.multiselect("转正机会", options=convert_list)

    # 执行 Python 过滤逻辑
    filtered_df = df.copy()
    if sel_cities:
        filtered_df = filtered_df[filtered_df['工作地点'].isin(sel_cities)]
    if sel_months:
        filtered_df = filtered_df[filtered_df['实习月数'].isin(sel_months)]
    if sel_convert:
        filtered_df = filtered_df[filtered_df['转正机会'].isin(sel_convert)]

    st.write(f"📊 筛选后符合要求的岗位：**{len(filtered_df)}** 个")
    st.dataframe(filtered_df.head(50), use_container_width=True)  # 预览前50条

    if st.button("🔥 开始 AI 智能匹配"):
        if filtered_df.empty:
            st.error("筛选后没有符合要求的岗位，请放宽筛选条件。")
        else:
            with st.spinner("AI 正在深度解析简历与岗位的契合度..."):
                # 读取简历内容
                with pdfplumber.open(cv_file) as pdf:
                    cv_text = "".join([page.extract_text() for page in pdf.pages])

                # 提取关键信息给 AI (取前15个岗位，防止 Token 溢出)
                jobs_to_ai = filtered_df[['职位名称', '职位描述', '任职要求']].head(15).reset_index().to_dict(
                    orient='records')

                prompt = f"""
                你现在是一位拥有 15 年经验的资深招聘专家，擅长从复杂的简历中挖掘人才与岗位的深度契合点。

                ### 评估背景
                【候选人简历】：
                {cv_text[:2500]}

                【待匹配岗位列表】：
                {json.dumps(jobs_to_ai, ensure_ascii=False)}

                ### 你的任务
                请基于以下逻辑框架，对简历与每个岗位进行深度匹配分析：

                1. **核心技能匹配度**：对比简历中的技术栈（如 Python, SQL, 财务建模等）与 JD 的硬性要求。
                2. **行业/项目相关性**：分析过往项目或实习经历在业务逻辑上是否与目标岗位一致。
                3. **软实力与潜力**：从奖项、社团经历中评估候选人的学习能力和执行力。

                ### 评分准则
                - **90-100分**：完美匹配，几乎无需培训即可上手。
                - **70-89分**：具备核心能力，但在特定经验或次要工具上略有欠缺。
                - **50-69分**：有一定基础，但需要大量带教或转岗跨度较大。
                - **50分以下**：基本不匹配。

                ### 输出要求
                请严格按 JSON 数组格式返回，不要包含任何前导语或总结语。格式如下：
                [
                  {{
                    "index": 岗位索引号,
                    "match_score": 整数评分,
                    "match_reason": "【核心优势】：[列出1-2点最匹配的经历或技能]；【潜在挑战】：[指出简历中缺少的关键要素或不足]；【综合判定】：[一句话说明为什么值得投递]。"
                  }}
                ]
                """

                try:
                    response = call_ai_with_retry(
                        client,
                        "deepseek-chat",
                        [{"role": "user", "content": prompt}]
                    )

                    # 1. 获取原始文本并清理（防止 AI 多嘴输出 ```json ... ```）
                    raw_content = response.choices[0].message.content.strip()
                    if raw_content.startswith("```json"):
                        raw_content = raw_content.replace("```json", "").replace("```", "").strip()

                    ai_res = json.loads(raw_content)

                    # 2. 强壮的解析逻辑：判断是列表还是字典
                    if isinstance(ai_res, list):
                        # 如果 AI 直接返回了 [{}, {}]
                        match_data = ai_res
                    elif isinstance(ai_res, dict):
                        # 如果 AI 返回了 {"results": [{}, {}]} 或 {"matches": []}
                        match_data = ai_res.get("results", ai_res.get("matches", list(ai_res.values())[0]))
                    else:
                        st.error("AI 返回的格式无法识别，请重试。")
                        st.stop()

                    # 3. 转换为 DataFrame
                    ai_df = pd.DataFrame(match_data)

                    # 检查 index 字段是否存在
                    if 'index' not in ai_df.columns:
                        st.error("AI 返回的数据中缺少 index 字段，请重新点击匹配。")
                        st.stop()

                    # 确保 index 类型一致
                    ai_df['index'] = ai_df['index'].astype(int)
                    final_df = filtered_df.reset_index().merge(ai_df, on='index', how='inner')

                    # 重新排序列，把匹配结果放最前面
                    cols = ['match_score', 'match_reason'] + [c for c in final_df.columns if
                                                              c not in ['match_score', 'match_reason', 'index']]
                    final_df = final_df[cols].sort_values(by='match_score', ascending=False)

                    st.success("✅ 匹配完成！已按匹配度降序排列。")
                    st.subheader("🎯 匹配结果推送 (含全字段信息)")
                    st.dataframe(final_df, use_container_width=True)

                    # 下载按钮
                    csv_data = final_df.to_csv(index=False).encode('utf-8-sig')
                    st.download_button("📥 下载完整分析报告 (CSV)", data=csv_data, file_name="实习匹配结果.csv")

                except Exception as e:
                    st.error(f"匹配失败，可能是 API 响应格式问题。错误详情：{e}")

# --- 5. 功能二：简历深度优化 (全量精修版 - 改进手动粘贴功能) ---
st.divider()
st.header("✍️ 第二步：简历深度优化")

# 初始化 Session State 缓存结果
if "refined_results" not in st.session_state:
    st.session_state.refined_results = None

# 1. 设置输入模式：PDF 提取 vs 手动输入
input_tab1, input_tab2 = st.tabs(["📄 从上传的 PDF 提取", "⌨️ 手动粘贴/修正内容"])
final_sections = {}  # 用于存储最终传递给 AI 的内容

with input_tab1:
    if cv_file:
        with pdfplumber.open(cv_file) as pdf:
            cv_raw_text = "".join([page.extract_text() for page in pdf.pages])

        # 调用你原来的切分函数
        auto_sections = split_resume_by_sections(cv_raw_text)
        st.success("✅ 已从 PDF 自动识别模块，你可以在下方进行内容微调（若内容分类不满足需求，可以选择“手动粘贴”）")

        # 允许用户在 Tab 1 里实时预览和修改提取到的内容
        for sec_name, sec_content in auto_sections.items():
            final_sections[sec_name] = st.text_area(f"确认模块：{sec_name}", value=sec_content, height=150,
                                                    key=f"auto_{sec_name}")
    else:
        st.info("请先在第一步上传 PDF 简历，或切换到“手动粘贴”模式。")

with input_tab2:
    st.markdown("##### 请按模块粘贴你想优化的内容")
    manual_sections = {
        "工作经历": st.text_area("工作/实习经历", placeholder="例如：2022.01-2023.01 XX公司 实习生\n1. 负责...",
                                 height=150),
        "项目经历": st.text_area("项目经历", placeholder="例如：XX数据分析项目\n使用Python进行...", height=150),
        "教育与技能": st.text_area("教育背景、技能及证书", placeholder="例如：英语六级、Python熟练...", height=100)
    }
    # 如果用户在手动模式下填写了内容，则覆盖自动提取的内容
    if any(manual_sections.values()):
        # 过滤掉空的模块
        final_sections = {k: v for k, v in manual_sections.items() if v.strip()}

# 2. 目标 JD 输入
target_jd = st.text_area("🎯 请贴入目标岗位要求 (JD)", height=150, placeholder="粘贴完整的任职要求和职位描述...")


# --- 修正后的“启动专家级精修”按钮逻辑 ---
if st.button("🪄 启动专家级精修"):
    if not final_sections or not target_jd:
        st.error("请确保已输入简历内容和目标 JD")
    else:
        # 1. 必须先初始化变量，防止下方逻辑跳过时报错
        refined_data = {}
        competency_analysis = "分析生成失败"
        final_summary = "总结生成失败"

        with st.status("🚀 专家正在深度重构中...", expanded=True) as status:

            # --- 第一阶段：岗位胜任力解析 (逻辑保持不变) ---
            status.write("🕵️ 正在进行岗位胜任力深度画像...")
            analysis_prompt = f"""
            你现在是资深猎头专家。请针对目标岗位 JD 进行深度解析：
            目标岗位: {target_jd}

            任务：提炼出企业最看重的【三项核心能力】，按以下格式输出：
            ### 【岗位胜任力解析】
            1. **专业能力 (Hard Skills)**：内容...
            2. **通用素质 (Soft Skills)**：内容...
            3. **业务潜力 (Potential)**：内容...
            """
            analysis_res = call_ai_with_retry(
                client, "deepseek-chat", [{"role": "user", "content": analysis_prompt}]
            )
            competency_analysis = analysis_res.choices[0].message.content

            for section_name, section_content in final_sections.items():
                if not section_content.strip(): continue
                status.write(f"正在重构：{section_name}...")

                specific_prompt = f"""
                你现在是一位拥有 15 年大厂招聘经验的【资深职业导师】，正在为候选人深度优化简历中的【{section_name}】模块。

                ### 1. 核心输入
                - **目标岗位要求 (JD)**：{target_jd}
                - **本模块原始内容**：{section_content}
                - **文风倾向**：{opt_style}
                - **重构深度**：{detail_depth}

                ### 2. 处理逻辑分配 (重要)
                请首先判断【{section_name}】的内容属性：
                - **属性 A (叙述类经历)**：包含工作经历、实习经历、项目经历、志愿者活动等。
                - **属性 B (信息/列表类)**：包含基本信息、教育背景、技能证书、自我评价等。

                ---

                ### 3. 任务输出要求

                #### 如果属于【属性 A (叙述类经历)】：
                请严格执行 STAR+XYZ 规则进行重写，并输出以下三个部分：

                ### 第一：核心任务：输出【简历精修对比表】
                你必须输出一个 Markdown 表格，表格有且仅有三列。
                
                ### 🚨 强制排版规则 (杜绝散乱拆分)
                （1） **一项目一行 (One Experience, One Row)**：
                   - 严禁将同一家公司或同一个项目下的多条描述拆分成多行输出。
                   - 必须将该项经历的所有原始描述合并在第一列的一个单元格内。
                   - 必须将该项经历的所有优化建议合并在第二列的一个单元格内。

                ### 🚨 每一列的“内容红线” (违者重罚)
                （1） **第一列【原始描述】**：
                   - 必须原封不动复制用户提供的原始简历短句。

                （2） **第二列【优化建议 (含 [XX] 占位符)】**：
                   - **这是最重要的列！** 只能填写重构后的**简历正文**。
                   - 必须使用 STAR/XYZ 公式，分点陈述（如：1. 2. 3.）。
                   - **严禁**在此列写任何解释性文字或理由。
                   - 示例：`1. 负责[XX]项目，通过[XX]工具提高效率[XX]%。<br>2. 协调[XX]人团队完成[XX]任务。`

                （3）**第三列【优化逻辑】**：
                   - **只能填写“为什么要这么改”的解释。**
                   - **严禁**在此列出现任何可以直接写进简历的描述性长句。
                   - 示例：`量化了项目成果，突出了技术栈与 JD 的匹配度。`

                ### 第二.：输出模板参考
                | 原始描述 | 优化建议 (含 [XX] 占位符) | 优化逻辑 |
                | :--- | :--- | :--- |
                | 负责财务报表制作 | 1. 独立完成[XX]份月度财务报表编制。<br>2. 使用[XX]工具优化核算流程。 | 量化了工作量，体现了工具熟练度。 |
                ---

                #### 如果属于【属性 B (信息/列表类)】：
                不需要进行操作，按照原文输出就行
                
                 ### 第三：基于“原始简历”的深度溯源提问 (严禁幻觉)
                 请审视【原始简历内容】，找出其中描述模糊、缺乏数据、或能够进一步支撑 JD 的潜在点。
                 请针对你在第一：核心任务：输出【简历精修对比表】中使用 **[XX]** 占位符的地方，向用户提出补全请求。
                 **注意：** - 严禁基于你改写后的内容（如虚构的金额、比例）提问。
                 - 必须针对原始经历中的模糊动词（如“负责、处理、协助”）进行追问。
                 - 提问应引导用户回忆：具体的金额、具体的件数、具体的工具使用频率。

                 ### 输出格式规范
                 1. **【简历精修对比表】** (表头：原始描述 | 优化建议(含 [XX] 占位符) | 优化逻辑)
                 2. **【数据补全清单】** (针对 [XX] 部分引导用户完善数据)
                 3. **【原始经历溯源提问】** (列出 3-5 个针对原始事实的细节追问)

                ---

                ### 4. 严禁事项 (红线)
                   （1） 禁止只输出标题，必须针对每一条经历给出重构后的长句。
                   （2） **严禁幻觉**：禁止虚构任何公司名称、项目金额、具体百分比。
                   （3）**严禁混淆**：不要在此处输出其他模块的内容。
                   （4） **格式纯净**：不要输出任何前导语（如“好的，我为您分析如下”），直接开始任务输出。
                """

                module_res = call_ai_with_retry(
                    client, "deepseek-chat", [{"role": "user", "content": specific_prompt}]
                )
                refined_data[section_name] = module_res.choices[0].message.content

            # --- 第三阶段：生成全局总结 (修复缩进和变量作用域) ---
            if refined_data:
                status.write("📝 正在生成全局求职策略建议...")
                all_refined = "\n".join(list(refined_data.values()))
                summary_prompt = f"针对以下精修后的内容，总结核心竞争力、面试建议并写一段100字自我评价：\n{all_refined[:2000]}"

                try:
                    # 确保在 try 块内进行 AI 调用
                    summary_res = call_ai_with_retry(
                        client, "deepseek-chat", [{"role": "user", "content": summary_prompt}]
                    )
                    final_summary = summary_res.choices[0].message.content
                except Exception as e:
                    final_summary = f"总结生成失败，错误原因：{e}"

            status.update(label="✅ 全量精修完成！", state="complete", expanded=False)

        st.session_state.refined_results = {
            "refined_data": refined_data,
            "competency_analysis": competency_analysis,
            "final_summary": final_summary
        }
        # 强制触发一次重绘，让结果立即显示
        st.rerun()


 # --- 结果展示与导出区 ---
if "refined_results" in st.session_state and st.session_state.refined_results:
    results = st.session_state.refined_results

    st.divider()  # 视觉分割线
    st.success("✨ 优化方案已生成！")

    # 1. 导出按钮
    col_dl, _ = st.columns([1, 2])
    with col_dl:
        st.download_button(
            "📥 一键导出 Word 报告",
            data=export_to_word(results["final_summary"], results["competency_analysis"], results["refined_data"]),
            file_name="简历深度优化报告.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    # 2. 展示全局总结和胜任力分析
    with st.expander("🎯 首席人才官：整体求职策略建议", expanded=True):
        st.markdown(results["final_summary"])

    st.markdown(results["competency_analysis"])

    # 3. 展示各模块 Tab
    st.subheader("📝 简历各模块精修建议")
    tabs = st.tabs(list(results["refined_data"].keys()))
    for i, (name, content) in enumerate(results["refined_data"].items()):
        with tabs[i]:
            st.markdown(content, unsafe_allow_html=True)

st.info("💡 **小贴士**：优化建议中的 **[XX]** 是 AI 为你预留的数据位")

# --- 6. 功能三：交互式 AI 助手 ---
st.divider()
st.subheader("💬 简历精修对话室")

if "messages" not in st.session_state:
    st.session_state.messages = []

# 显示聊天历史
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if chat_input := st.chat_input("例如：'帮我把这段经历写得更有领导力一点'"):
    st.session_state.messages.append({"role": "user", "content": chat_input})
    with st.chat_message("user"):
        st.markdown(chat_input)

    with st.chat_message("assistant"):
        response = call_ai_with_retry(
            client,
            "deepseek-chat",
            [{"role": "system", "content": "你是一个资深简历优化专家。"}] + st.session_state.messages
        )
        ans = response.choices[0].message.content
        st.markdown(ans)
    st.session_state.messages.append({"role": "assistant", "content": ans})