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

# --- 1. 基础配置与安全校验 ---
try:
    DEEPSEEK_API_KEY = st.secrets["DEEPSEEK_API_KEY"]
    SQL_SHEET_URL = st.secrets["SQL_SHEET_URL"]
except Exception as e:
    st.error("❌ 缺失 Secrets 配置（DEEPSEEK_API_KEY 或 SQL_SHEET_URL）")
    st.stop()

# 建立连接
conn = st.connection("gsheets", type=GSheetsConnection)
client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")


# --- 2. 核心后端逻辑函数 ---

def verify_user(license_key):
    """验证激活码：检查是否存在、状态及额度"""
    try:
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
    """
    扣除额度核心函数
    修复：强制将 Used_Count 转为 float，防止 Pandas 自动截断小数位导致 Google Sheets 回写变整数
    """
    try:
        user_df = conn.read(spreadsheet=SQL_SHEET_URL, worksheet="users", ttl=0)
        # 核心修复：确保列类型为 float
        user_df['Used_Count'] = user_df['Used_Count'].astype(float)

        idx = user_df[user_df['License_Key'] == license_key].index[0]
        new_used = float(user_df.at[idx, 'Used_Count']) + amount
        user_df.at[idx, 'Used_Count'] = new_used

        # 写回数据库
        conn.update(spreadsheet=SQL_SHEET_URL, worksheet="users", data=user_df)

        # 同步更新本地 Session 状态，确保侧边栏余额实时刷新
        if "user_info" in st.session_state:
            st.session_state.user_info['Used_Count'] = new_used
        return True
    except Exception as e:
        st.error(f"计费系统异常: {e}")
        return False


def call_ai_with_retry(client, model, messages, max_retries=3, delay=2):
    """带有指数退避机制的 AI 调用，防止频率限制错误"""
    for i in range(max_retries):
        try:
            return client.chat.completions.create(model=model, messages=messages)
        except Exception as e:
            if ("429" in str(e) or "rate_limit" in str(e).lower()) and i < max_retries - 1:
                wait_time = delay * (2 ** i)
                time.sleep(wait_time)
                continue
            raise e


def export_to_word(summary, analysis, refined_data):
    """导出 Word 报告，处理 HTML 换行符以保持排版整洁"""
    doc = Document()
    style = doc.styles['Normal']
    style.font.name = '微软雅黑'
    style.font.size = Pt(10)

    doc.add_heading('简历深度优化报告', 0)
    doc.add_heading('一、首席人才官：整体求职策略建议', level=1)
    doc.add_paragraph(summary)
    doc.add_heading('二、岗位胜任力深度画像', level=1)
    doc.add_paragraph(analysis)
    doc.add_heading('三、简历各模块精修建议', level=1)

    for section_name, content in refined_data.items():
        doc.add_heading(f'模块：{section_name}', level=2)
        # 将表格符号简单处理，并将 <br> 标签转为 Word 换行
        clean_text = content.replace('<br>', '\n').replace('|', '').replace('---', '')
        doc.add_paragraph(clean_text)
        doc.add_page_break()

    bio = io.BytesIO()
    doc.save(bio)
    return bio.getvalue()


def split_resume_by_sections(text):
    """使用正则表达式自动切分简历模块"""
    patterns = {
        "工作经历": r"(工作经历|实习|实习经历|Experience|Work)",
        "项目经历": r"(项目经历|项目经验|个人项目|Projects)",
        "技能证书": r"(专业技能|技能特长|语言能力|证书|Skills)",
        "自我评价": r"(自我评价|个人总结|About Me)"
    }
    matches = sorted([(m.start(), s) for s, p in patterns.items() for m in re.finditer(p, text, re.I)])
    sections = {}
    if matches and matches[0][0] > 0:
        sections["基本信息"] = text[:matches[0][0]].strip()
    for i in range(len(matches)):
        start_pos, section_name = matches[i]
        end_pos = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        sections[section_name] = text[start_pos:end_pos].strip()
    return sections if sections else {"完整简历": text}


# --- 3. 页面 UI 基本配置 ---
st.set_page_config(page_title="暑期实习优化利器", layout="wide", initial_sidebar_state="expanded")
st.title("🚀 暑期实习岗位精准匹配与优化工具")

with st.expander("🛡️ 隐私保护与数据安全说明", expanded=False):
    st.info("本工具承诺不存储文件，所有数据随网页关闭自动销毁。")

# --- 4. 侧边栏授权管理 ---
with st.sidebar:
    st.header("🔑 访问授权")
    user_code = st.text_input("请输入您的专属激活码", type="password")

    if not user_code:
        if "user_info" in st.session_state:
            del st.session_state.user_info
        st.info("💡 请输入激活码以解锁功能。")
        st.stop()

    if "user_info" not in st.session_state or st.session_state.get("last_verified_code") != user_code:
        with st.spinner("正在验证权限..."):
            user_data, msg = verify_user(user_code)
            if user_data is not None:
                st.session_state.user_info = user_data.to_dict()
                st.session_state.last_verified_code = user_code
            else:
                st.error(msg)
                st.stop()

    current_user = st.session_state.user_info
    st.success(f"欢迎，{current_user['User_Name']}！")
    remaining_count = current_user['Total_Count'] - current_user['Used_Count']
    st.metric("剩余可用额度", f"{remaining_count} 次")

    st.divider()
    st.header("🎨 简历定制偏好")
    opt_style = st.radio("文风倾向", ["稳重务实型", "极简干练型", "充满活力型"], index=1)
    detail_depth = st.select_slider("细节挖掘深度", options=["点到为止", "标准修饰", "深度重构"], value="标准修饰")

# --- 5. 功能一：精准匹配 ---
st.header("📅 第一步：岗位匹配")
try:
    jobs_df = conn.read(spreadsheet=SQL_SHEET_URL, worksheet="jobs", ttl=600)
    st.success("✅ 岗位库已同步")
except:
    st.error("岗位库同步失败，请检查 Sheet 名称。")
    st.stop()

cv_file = st.file_uploader("上传你的简历 (PDF)", type=["pdf"])

if cv_file:
    st.subheader("🔍 岗位精准筛选")
    c1, c2, c3 = st.columns(3)
    with c1:
        city_list = jobs_df['工作地点'].dropna().unique().tolist() if '工作地点' in jobs_df.columns else []
        sel_cities = st.multiselect("实习区域", options=city_list)
    with c2:
        month_list = jobs_df['实习月数'].dropna().unique().tolist() if '实习月数' in jobs_df.columns else []
        sel_months = st.multiselect("实习时长 (月)", options=month_list)
    with c3:
        convert_list = jobs_df['转正机会'].dropna().unique().tolist() if '转正机会' in jobs_df.columns else []
        sel_convert = st.multiselect("转正机会", options=convert_list)

    filtered_df = jobs_df.copy()
    if sel_cities: filtered_df = filtered_df[filtered_df['工作地点'].isin(sel_cities)]
    if sel_months: filtered_df = filtered_df[filtered_df['实习月数'].isin(sel_months)]
    if sel_convert: filtered_df = filtered_df[filtered_df['转正机会'].isin(sel_convert)]

    st.write(f"📊 筛选后岗位数量：**{len(filtered_df)}**")
    st.dataframe(filtered_df.head(50), use_container_width=True)

    if st.button("🔥 开始 AI 智能匹配 (消耗 1 额度)"):
        if filtered_df.empty:
            st.error("筛选后没有符合要求的岗位。")
        else:
            with st.spinner("AI 正在深度解析匹配度..."):
                with pdfplumber.open(cv_file) as pdf:
                    cv_text = "".join([page.extract_text() for page in pdf.pages])

                jobs_to_ai = filtered_df[['职位名称', '职位描述', '任职要求']].head(15).reset_index().to_dict(
                    orient='records')

                match_prompt = f"你是一个招聘专家。简历内容：{cv_text[:2000]}。岗位列表：{json.dumps(jobs_to_ai, ensure_ascii=False)}。请给出 0-100 的 match_score 和 match_reason。严格按 JSON 数组格式返回，含 index 字段。"

                try:
                    match_res = call_ai_with_retry(client, "deepseek-chat", [{"role": "user", "content": match_prompt}])
                    raw_content = match_res.choices[0].message.content.strip()
                    if raw_content.startswith("```json"):
                        raw_content = raw_content.replace("```json", "").replace("```", "").strip()

                    ai_match_results = json.loads(raw_content)
                    ai_df = pd.DataFrame(ai_match_results)
                    ai_df['index'] = ai_df['index'].astype(int)

                    final_match_df = filtered_df.reset_index().merge(ai_df, on='index', how='inner').sort_values(
                        by='match_score', ascending=False)

                    # 计费：只调用函数，不进行额外的 += 1 操作
                    if deduct_usage(user_code, amount=1.0):
                        st.success("✅ 匹配完成！")
                        st.dataframe(final_match_df, use_container_width=True)
                except Exception as e:
                    st.error(f"匹配解析失败: {e}")

# --- 6. 功能二：简历深度优化 (JSON 稳定渲染版) ---
st.divider()
st.header("✍️ 第二步：简历深度优化")
if "refined_results" not in st.session_state:
    st.session_state.refined_results = None

tab_in1, tab_in2 = st.tabs(["📄 从 PDF 自动提取", "⌨️ 手动粘贴/微调"])
final_sections = {}

with tab_in1:
    if cv_file:
        with pdfplumber.open(cv_file) as pdf:
            extracted_text = "".join([p.extract_text() for p in pdf.pages])
        auto_secs = split_resume_by_sections(extracted_text)
        for name, content in auto_secs.items():
            final_sections[name] = st.text_area(f"模块：{name}", value=content, height=150, key=f"auto_{name}")
    else:
        st.info("请先上传简历 PDF。")

with tab_in2:
    manual_work = st.text_area("经历/项目描述", placeholder="粘贴你想优化的工作或项目经历...", height=150)
    manual_skill = st.text_area("技能证书/评价", placeholder="粘贴技能、证书等信息...", height=100)
    if manual_work or manual_skill:
        final_sections = {"核心经历": manual_work, "其他信息": manual_skill}

target_jd = st.text_area("🎯 目标岗位 JD 要求", placeholder="粘贴目标岗位的描述和要求...", height=150)

if st.button("🪄 启动专家级精修 (消耗 1 额度)"):
    if not final_sections or not target_jd:
        st.error("请确保输入了简历内容和目标岗位 JD。")
    else:
        refined_data = {}
        comp_analysis = ""
        final_summary = ""

        with st.status("🚀 专家正在深度重构中...", expanded=True) as status:
            # 1. 岗位画像分析
            status.write("🕵️ 正在进行岗位胜任力深度画像...")
            ana_prompt = f"你现在是资深猎头，请深度解析以下岗位 JD 的核心胜任力要求：{target_jd}"
            ana_resp = call_ai_with_retry(client, "deepseek-chat", [{"role": "user", "content": ana_prompt}])
            comp_analysis = ana_resp.choices[0].message.content

            # 2. 模块级精修 (使用 JSON 模式确保格式稳定)
            for s_name, s_content in final_sections.items():
                if not s_content.strip(): continue
                status.write(f"正在重构模块：{s_name}...")

                # 强化的 JSON 输出 Prompt
                specific_json_prompt = f"""
                你现在是 15 年经验的简历优化专家。请深度优化简历中的【{s_name}】。
                JD：{target_jd} | 文风：{opt_style} | 深度：{detail_depth}

                输出规则：
                1. 经历类(TYPE_A): 使用 STAR+XYZ，[XX]占位，合并同一项目为一行。
                2. 列表类(TYPE_B): 保持事实，备注“不予修改”。
                必须直接输出纯 JSON 对象（不含 ```json 标签）：
                {{
                  "type": "A",
                  "data": [{{ "orig": "原始描述", "ref": "优化建议(分点并使用<br>换行)", "log": "优化逻辑" }}],
                  "qs": ["细节追问1", "细节追问2"]
                }}
                """

                mod_res = call_ai_with_retry(client, "deepseek-chat",
                                             [{"role": "user", "content": specific_json_prompt}])

                # 核心解析与渲染逻辑
                try:
                    raw_json = mod_res.choices[0].message.content.strip()
                    # 清理可能存在的代码块标签
                    raw_json = re.sub(r'^```json\s*|\s*```$', '', raw_json, flags=re.MULTILINE)
                    res_json = json.loads(raw_json)

                    # 将 JSON 动态渲染为 Markdown 表格，以便用户查看
                    md_display = "#### 📊 简历精修对比表\n\n"
                    if res_json.get("type") == "A":
                        md_display += "| 原始描述 | 优化建议 (含 [XX] 占位符) | 优化逻辑 |\n| :--- | :--- | :--- |\n"
                        for item in res_json.get("data", []):
                            md_display += f"| {item['orig']} | {item['ref']} | {item['log']} |\n"
                    else:
                        md_display += "| 原始描述 | 备注 |\n| :--- | :--- |\n"
                        for item in res_json.get("data", []):
                            md_display += f"| {item['orig']} | {item['log']} |\n"

                    md_display += "\n\n#### 💡 深度溯源提问（针对 [XX] 位）\n"
                    for q in res_json.get("qs", []): md_display += f"* {q}\n"

                    refined_data[s_name] = md_display
                except:
                    # 兜底：如果 JSON 解析失败，则显示原文
                    refined_data[s_name] = mod_res.choices[0].message.content

            # 3. 生成全局总结
            status.write("📝 正在生成全局策略...")
            sum_prompt = f"针对以上精修建议，给出一个 100 字左右的自我评价：{str(refined_data)[:1500]}"
            sum_res = call_ai_with_retry(client, "deepseek-chat", [{"role": "user", "content": sum_prompt}])
            final_summary = sum_res.choices[0].message.content

            # 4. 执行计费
            if deduct_usage(user_code, amount=1.0):
                status.update(label="✅ 简历专家级精修完成！", state="complete")

        st.session_state.refined_results = {
            "refined_data": refined_data,
            "comp_analysis": comp_analysis,
            "final_summary": final_summary
        }
        st.rerun()

# 结果展示区
if st.session_state.refined_results:
    res = st.session_state.refined_results
    st.divider()
    st.success("✨ 优化方案已生成！")

    st.download_button(
        "📥 一键导出 Word 报告",
        data=export_to_word(res["final_summary"], res["comp_analysis"], res["refined_data"]),
        file_name="简历深度优化报告.docx"
    )

    with st.expander("🎯 整体求职策略建议", expanded=True):
        st.markdown(res["final_summary"])
    st.markdown(res["comp_analysis"])

    st.subheader("📝 简历模块精修细节")
    tabs = st.tabs(list(res["refined_data"].keys()))
    for i, (name, content) in enumerate(res["refined_data"].items()):
        with tabs[i]:
            # 开启 HTML 支持，以便渲染表格内的 <br> 换行符
            st.markdown(content, unsafe_allow_html=True)

# --- 7. 功能三：交互式 AI 助手 (带记忆的对话模式) ---
st.divider()
st.subheader("💬 简历精修对话室")
st.info("💡 对话模式每次提问消耗 **0.5** 额度。AI 已获知您的岗位及上述精修结果。")

if "messages" not in st.session_state:
    st.session_state.messages = []

# 显示对话历史
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if chat_input := st.chat_input("您可以继续追问，例如：‘帮我把财务实习的第一条改得更稳重些’"):
    # 检查余额
    if st.session_state.user_info['Used_Count'] >= st.session_state.user_info['Total_Count']:
        st.warning("⚠️ 额度不足，无法继续对话。")
        st.stop()

    # 构建上下文注入：让 AI 记得 JD 和之前的优化内容
    history_context = ""
    if st.session_state.refined_results:
        history_context = f"目标JD: {target_jd}\n优化成果摘要: {str(st.session_state.refined_results['refined_data'])[:1000]}"

    sys_prompt = f"你是一个资深简历导师。背景信息：{history_context}\n请基于此背景回答用户追问，保持专业简练。"

    # 记录并显示用户输入
    st.session_state.messages.append({"role": "user", "content": chat_input})
    with st.chat_message("user"):
        st.markdown(chat_input)

    # 调用 AI 响应
    with st.chat_message("assistant"):
        with st.spinner("正在思考..."):
            full_msgs = [{"role": "system", "content": sys_prompt}] + st.session_state.messages
            resp = call_ai_with_retry(client, "deepseek-chat", full_msgs)
            answer = resp.choices[0].message.content
            st.markdown(answer)

            # 成功后按 0.5 额度扣费
            if deduct_usage(user_code, amount=0.5):
                st.toast("已消耗 0.5 次额度", icon="💰")

            st.session_state.messages.append({"role": "assistant", "content": answer})