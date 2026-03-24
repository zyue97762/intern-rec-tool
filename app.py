import streamlit as st
import pandas as pd
import pdfplumber
import json
from openai import OpenAI
import re
from docx import Document
from docx.shared import Pt
import io
import time
from streamlit_gsheets import GSheetsConnection



# 从 Secrets 获取关键配置
try:
    DEEPSEEK_API_KEY = st.secrets["DEEPSEEK_API_KEY"]
    SQL_SHEET_URL = st.secrets["SQL_SHEET_URL"]
except Exception as e:
    st.error("❌ 缺失 Secrets 配置（DEEPSEEK_API_KEY 或 SQL_SHEET_URL）")
    st.stop()

# 建立 GSheets 连接
conn = st.connection("gsheets", type=GSheetsConnection)


# --- 2. 权限与计费核心函数 ---

def verify_user(license_key):
    """验证激活码：检查是否存在、是否激活、额度是否足够"""
    try:
        # 读取用户表 (ttl=0 保证实时获取最新额度)
        user_df = conn.read(spreadsheet=SQL_SHEET_URL, worksheet="users", ttl=0)
        user_data = user_df[user_df['License_Key'] == license_key]

        if user_data.empty:
            return None, "❌ 激活码无效"

        user_info = user_data.iloc[0]
        if user_info['Status'] != 'active':
            return None, "🚫 该激活码已被禁用"

        if user_info['Used_Count'] >= user_info['Total_Count']:
            return None, "⚠️ 额度已用完，请联系管理员续费"

        return user_info, "✅ 验证通过"
    except Exception as e:
        return None, f"校验出错: {e}"


def deduct_usage(license_key, amount=1.0):
    try:
        # 1. 实时读取最新数据
        user_df = conn.read(spreadsheet=SQL_SHEET_URL, worksheet="users", ttl=0)

        # 【核心修复】强制将 Used_Count 列转为浮点数类型，防止赋值时被截断
        user_df['Used_Count'] = user_df['Used_Count'].astype(float)

        idx = user_df[user_df['License_Key'] == license_key].index[0]

        # 2. 计算新额度
        current_used = float(user_df.at[idx, 'Used_Count'])
        new_used = current_used + amount

        # 现在赋值给 float 类型的列，5.5 就不会变成 5 了
        user_df.at[idx, 'Used_Count'] = new_used

        # 3. 写回 Google Sheets
        conn.update(spreadsheet=SQL_SHEET_URL, worksheet="users", data=user_df)

        # 4. 同步更新本地缓存
        if "user_info" in st.session_state:
            st.session_state.user_info['Used_Count'] = new_used

        return True
    except Exception as e:
        st.error(f"计费系统异常: {e}")
        return False



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
st.set_page_config(page_title=" 暑期实习求职利器", layout="wide", initial_sidebar_state="expanded")
st.title("🚀 暑期实习岗位精准匹配与优化工具")

# --- 2. 隐私保护声明 ---
with st.expander("🛡️ 隐私保护与数据安全说明", expanded=False):
    st.info("""
    **本工具郑重承诺：**
    1. **不留痕迹**：你上传的简历和岗位表仅在服务器内存中实时处理。
    2. **不存储文件**：一旦你刷新或关闭网页，所有数据将自动彻底销毁。
    3. **脱敏建议**：你可以删除简历中的手机号、住址等敏感信息，不影响 AI 评估。
    """)

# --- 3. 侧边栏：授权管理 ---

with st.sidebar:
    st.header("🔑 访问授权")
    user_code = st.text_input("请输入您的专属激活码", type="password", help="联系管理员获取")

    if not user_code:
        # 如果清空了输入框，也重置验证状态
        if "user_info" in st.session_state:
            del st.session_state.user_info
        st.info("💡 请输入激活码以解锁简历优化功能。")
        st.stop()

    # 只有在以下情况才去读取 Google Sheets:
    # 1. session_state 里没有用户信息
    # 2. 用户输入的 code 和上次验证成功的 code 不一致
    if "user_info" not in st.session_state or st.session_state.get("last_verified_code") != user_code:

        with st.spinner("正在验证权限..."):
            user_data, msg = verify_user(user_code)

            if user_data is not None:
                # 验证成功，存入“记忆”
                st.session_state.user_info = user_data.to_dict()  # 转为字典方便存储
                st.session_state.last_verified_code = user_code
            else:
                # 验证失败，显示错误并停止
                st.error(msg)
                if "user_info" in st.session_state:
                    del st.session_state.user_info
                st.stop()

    # 从“记忆”中直接读取用户信息，不再请求网络
    current_user = st.session_state.user_info

    st.success(f"欢迎回来，{current_user['User_Name']}！")

    # 计算剩余额度（注意：扣费后需要手动更新这个显示）
    remaining = current_user['Total_Count'] - current_user['Used_Count']
    st.metric("剩余可用额度", f"{remaining} 次")

    st.divider()
    st.header("🎨 简历定制偏好")
    opt_style = st.radio("文风倾向", ["稳重务实型", "极简干练型", "充满活力型"], index=1)
    detail_depth = st.select_slider("细节挖掘深度", options=["点到为止", "标准修饰", "深度重构"], value="标准修饰")

# 初始化 AI 客户端 (统一使用 Secret)
client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")


# --- 5. 功能区 ---

# 功能一：精准匹配
st.header("📅 第一步：岗位匹配")
try:
    df = conn.read(spreadsheet=SQL_SHEET_URL, worksheet="jobs", ttl=600) # 假设岗位在 jobs 表
    st.success("✅ 岗位库已同步")
except:
    st.error("无法同步岗位库，请检查表格名称是否为 'jobs'")
    st.stop()

cv_file = st.file_uploader("上传你的简历 (PDF)", type=["pdf"])

if cv_file:
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

    if st.button("🔥 开始 AI 智能匹配(消耗1额度)"):
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

                    if deduct_usage(user_code, amount=1.0):
                        # 同步更新本地缓存，这样页面不需要重新读表也能显示正确的余额
                        pass
                    st.success("✅ 匹配完成！已按匹配度降序排列(本次消耗 1 次额度)")
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
if st.button("🪄 启动专家级精修（消耗1额度）"):
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
                你现在是一位拥有 15 年大厂招聘经验的【资深职业导师】。请深度重构简历中的【{section_name}】模块。

                ### 1. 核心输入
                - **目标岗位要求 (JD)**：{target_jd}
                - **本模块原始内容**：{section_content}
                - **文风倾向**：{opt_style}
                - **重构深度**：{detail_depth}

                ### 2. 处理规则
                1. **分类逻辑**：
                   - **TYPE_A (叙述类)**：工作/项目经历。需使用 STAR+XYZ 法则，严禁虚构数据，用 [XX] 代替。必须将“同一项目”的所有描述合并在一个条目内。
                   - **TYPE_B (信息类)**：教育/技能。保持事实，不做润色，统一备注“基于事实不予修改”。
                2. **严禁幻觉**：禁止捏造任何具体的百分比、金额或公司名称。
                3. **溯源提问**：针对原始描述中的模糊动词（如“负责”、“协助”），提出 3-5 个细节追问以补全 [XX]。

                ### 3. 强制输出格式 (JSON)
                你必须直接输出一个符合以下结构的 JSON 对象，不要包含任何 Markdown 代码块标签（如 ```json）或多余的解释文字：

                {{
                  "content_type": "A 或 B",
                  "table_data": [
                    {{
                      "original_desc": "原始内容（必须完整复制原始段落）",
                      "refined_content": "优化后的简历正文（属性A分点并使用 <br> 换行；属性B原样返回）",
                      "analysis_logic": "属性A写优化逻辑；属性B写固定备注"
                    }}
                  ],
                  "follow_up_questions": [
                    "针对原始事实的追问1",
                    "针对原始事实的追问2"
                  ]
                }}

                ### 4. 示例参考 (Few-Shot)
                **输入内容为叙述类时，你应该返回：**
                {{
                  "content_type": "A",
                  "table_data": [
                    {{
                      "original_desc": "负责财务报表制作，协助进行审计工作。",
                      "refined_content": "1. 独立编制[XX]份月度财务报表，确保数据准确率达[XX]%。<br>2. 协助[XX]家子公司进行年度审计，整理底稿[XX]份。",
                      "analysis_logic": "通过 XYZ 原则量化工作量，突出了财务核算的专业度。"
                    }}
                  ],
                  "follow_up_questions": ["您平均每月制作多少份报表？", "在审计中您具体负责哪一类底稿的整理？"]
                }}
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


            if deduct_usage(user_code, amount=1.0):
                # 同步更新本地缓存，这样页面不需要重新读表也能显示正确的余额
                pass
            status.update(label="✅ 全量精修完成！（本次消耗1次额度）", state="complete", expanded=False)

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
# 温馨提示
st.info("💡 **计费说明**：对话模式每次提问消耗 **0.5** 次额度。")

if "messages" not in st.session_state:
    st.session_state.messages = []

# 显示聊天历史
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# 聊天输入框
if chat_input := st.chat_input("针对优化结果，你可以继续追问"):

    # 1. 检查余额 (保持不变)
    current_u = st.session_state.get("user_info")
    if current_u['Used_Count'] >= current_u['Total_Count']:
        st.warning("⚠️ 您的额度已耗尽，请联系管理员续费后再对话。")
        st.stop()

    # 2. 【核心修改】构建上下文背景
    # 尝试从之前的步骤中抓取数据
    context_info = ""

    # 获取 JD
    current_jd = target_jd if 'target_jd' in locals() else "未提供"

    # 获取之前的精修建议
    refined_summary = ""
    if "refined_results" in st.session_state and st.session_state.refined_results:
        res = st.session_state.refined_results
        refined_summary = f"你之前给出的优化策略是：{res['final_summary']}\n"
        # 也可以把各模块的精修点简要带入
        for sec, content in res['refined_data'].items():
            refined_summary += f"--- {sec} 模块优化建议 ---\n{content[:500]}...\n"

    # 构建一个强大的 System Message
    system_prompt = f"""你是一个资深简历优化专家。
你正在协助用户进行简历修饰。以下是当前任务的背景信息，请务必基于这些信息回答用户的追问：

【目标岗位 JD】：
{current_jd}

【之前的优化成果】：
{refined_summary}

请根据以上背景，结合用户的具体提问，给出针对性、专业且简洁的修改建议。"""

    # 3. 展示并记录用户消息
    st.session_state.messages.append({"role": "user", "content": chat_input})
    with st.chat_message("user"):
        st.markdown(chat_input)

    # 4. AI 响应
    with st.chat_message("assistant"):
        with st.spinner("专家正在思考中..."):
            try:
                # 【核心修改】将 system_prompt 作为第一条消息发送
                full_messages = [{"role": "system", "content": system_prompt}] + st.session_state.messages

                response = call_ai_with_retry(
                    client,
                    "deepseek-chat",
                    full_messages
                )
                ans = response.choices[0].message.content
                st.markdown(ans)

                # 5. 成功后执行扣费 (保持不变)
                if deduct_usage(user_code, amount=0.5):
                    st.toast(f"已消耗 0.5 次额度", icon="💰")

                st.session_state.messages.append({"role": "assistant", "content": ans})

            except Exception as e:
                st.error(f"对话中断，请重试。错误信息：{e}")