import streamlit as st
import pandas as pd
import pdfplumber
from openai import OpenAI

# --- 1. 页面基本配置 ---
st.set_page_config(page_title="暑期实习求职利器", layout="wide")
st.title("🚀 暑期实习岗位精准匹配与优化工具")

# --- 新增：隐私声明 ---
with st.expander("🛡️ 隐私保护与数据安全说明", expanded=False):
    st.info("""
    **本工具郑重承诺：**
    1. **不留痕迹**：你上传的简历（PDF）和岗位表（Excel）仅在服务器**内存**中进行实时处理。
    2. **不存储文件**：我们没有配置任何数据库或硬盘存储，一旦你**刷新或关闭网页**，所有上传的数据将自动彻底销毁。
    3. **脱敏建议**：为了极致安全，你可以在上传前将简历中的“手机号”、“具体住址”等敏感信息删除，这不会影响 AI 对你背景的评估。
    4. **加密传输**：本平台（Streamlit Cloud）默认启用 HTTPS 加密，确保数据传输过程不被截获。
    """)

# --- 2. 侧边栏：权限验证与配置 ---
with st.sidebar:
    st.header("🔑 授权与设置")
    auth_code = st.text_input("请输入授权码", type="password")
    api_key = st.text_input("请输入你的 DeepSeek API Key", type="password")
    model_choice = "deepseek-chat"  # 默认使用 deepseek 模型

# 修改后（使用代号）：
if auth_code != st.secrets["MY_AUTH_CODE"]:
    st.warning("请在侧边栏输入正确的授权码以解锁功能。")
    st.stop()


if not api_key:
    st.info("请输入 API Key 以启用 AI 功能。")
    st.stop()

# 初始化 AI 客户端
client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

# --- 3. 功能一：上传与匹配 ---
st.header("📅 第一步：岗位匹配")
col1, col2 = st.columns(2)

with col1:
    job_file = st.file_uploader("上传岗位 Excel 表", type=["xlsx"])
with col2:
    cv_file = st.file_uploader("上传你的简历 (PDF)", type=["pdf"])

if job_file and cv_file:
    # 读取 Excel
    df = pd.read_excel(job_file)
    st.write("已加载岗位信息：", df.head(3))  # 显示前3行

    # 读取简历内容
    with pdfplumber.open(cv_file) as pdf:
        cv_text = "".join([page.extract_text() for page in pdf.pages])

    if st.button("开始精准匹配"):
        with st.spinner("AI 正在分析中..."):
            # 这里简化逻辑：将前5个岗位发给 AI 匹配
            jobs_summary = df.to_string()
            prompt = f"以下是简历内容：\n{cv_text}\n\n以下是岗位列表：\n{jobs_summary}\n请根据简历匹配最合适的3个岗位，并以表格形式给出：岗位名称、匹配度(0-100)、推荐理由。"

            response = client.chat.completions.create(
                model=model_choice,
                messages=[{"role": "user", "content": prompt}]
            )
            st.markdown("### 🎯 匹配结果推送")
            st.write(response.choices[0].message.content)

# --- 4. 功能二：简历优化 ---
st.divider()
st.header("✍️ 第二步：针对性优化简历")
target_job = st.text_area("请输入你选定的目标岗位要求 (JD)")

if st.button("生成优化建议"):
    if target_job and cv_text:
        with st.spinner("正在逐句精修..."):
            optimize_prompt = f"目标岗位：{target_job}\n当前简历：{cv_text}\n请结合我的真实经历，针对该岗位要求，用 STAR 法则优化我的简历经历，使其更具竞争力。"
            response = client.chat.completions.create(
                model=model_choice,
                messages=[{"role": "user", "content": optimize_prompt}]
            )
            st.subheader("💡 优化建议")
            st.write(response.choices[0].message.content)
            st.info("你可以根据建议在下方直接与 AI 进一步沟通修改。")
    else:
        st.error("请先上传简历并输入目标岗位信息。")

# --- 5. 交互对话框 ---
st.divider()
st.subheader("💬 与 AI 助手深度交流")
if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("比如问：‘帮我把这段经历写得更专业一点’"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        response = client.chat.completions.create(
            model=model_choice,
            messages=st.session_state.messages
        )
        answer = response.choices[0].message.content
        st.markdown(answer)
    st.session_state.messages.append({"role": "assistant", "content": answer})
