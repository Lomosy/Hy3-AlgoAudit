from __future__ import annotations

from openai import OpenAI

from .config import Settings,load_settings

class Hy3Client:
    """基于 OpenAI 兼容接口的Hy3 调用封装
    
    统一管理推理强度、采样参数、超时与重传，
    供solver / evaluator 等复用
    """
    
    def __init__(self,settings:Settings | None= None):
        self.settings = settings or load_settings()
        self.client = OpenAI(
            base_url=self.settings.base_url,
            api_key = self.settings.api_key,
            timeout=self.settings.timeout_seconds,
            max_retries= self.settings.max_retries,
        )
    
    def chat(
        self,
        prompt:str,
        *,
        system: str| None = None,
        reasoning_effort: str |None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        n: int = 1,
    ) -> str | list |None:
        """ 发送一次对话请求，返回模型文本回复
        reasoning_effort 可按次覆盖默认值（no_think / low / high)，
        简单调用省资源、深度推理给足预算

        n 的说明：早期版本这里硬编码 n=3，但只返回 choices[0]，
        等于每次调用都付 3 倍的 token 却只用 1 份结果。默认改为 n=1。
        需要 Pass@k 这类多样本采样时显式传 n>1，并改用 chat_many() 取回全部候选。

        注：多数兼容 OpenAI 协议的后端在 n>1 时会退化成「只生成一条」，
        做采样前应先验证后端是否真的返回多条。
        """
        messages = []
        if system:
            messages.append({"role":"system","content":system})
        
        messages.append({"role":"user","content":prompt})
        
        resp = self.client.chat.completions.create(
            model = self.settings.model_name,
            messages=messages,
            temperature=temperature if temperature is not None else self.settings.temperature,
            top_p= top_p if top_p is not None else self.settings.top_p,
            # OpenAI SDK 的标准扩展通道
            # 参数会原样透传给后端服务
            # 兼容 vLLM/SGLang 部署和云端 API 两种形态
            extra_body={
                "reasoning_effort": reasoning_effort or self.settings.reasoning_effort,
            },
            n=n,
        )
        
        return resp.choices[0].message.content

    def chat_many(
        self,
        prompt:str,
        *,
        n: int = 3,
        system: str| None = None,
        reasoning_effort: str |None = None,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> list[str]:
        """多样本采样：返回全部 n 条回复，供 Pass@k / 自一致性投票使用"""
        messages = []
        if system:
            messages.append({"role":"system","content":system})
        messages.append({"role":"user","content":prompt})

        resp = self.client.chat.completions.create(
            model = self.settings.model_name,
            messages=messages,
            temperature=temperature if temperature is not None else self.settings.temperature,
            top_p= top_p if top_p is not None else self.settings.top_p,
            extra_body={
                "reasoning_effort": reasoning_effort or self.settings.reasoning_effort,
            },
            n=n,
        )
        return [choice.message.content or "" for choice in resp.choices]
        