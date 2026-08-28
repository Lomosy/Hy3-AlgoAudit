"""
连通性自检：验证 .env 配置与 Hy3调用链路是否正常
"""

from hy3_algoaudit.llm import Hy3Client

def main() -> None:
    print("加载配置并连接 Hy3 ...")
    
    client =Hy3Client()
    
    print(f"model = {client.settings.model_name}")
    print(f"base = {client.settings.base_url}")
    
    reply = client.chat("请直接回答：八皇后问题的几种解法", reasoning_effort="no_think")
    print("输出choice数量：",len(reply))
    print(f"模型回复：{reply}")
    
if __name__ == "__main__":
    main()