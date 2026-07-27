def llm_call_stream(prompt: str, label: str = "", max_tokens: int = 4096) -> str:
    t0 = time.time()
    # print("prompt:")
    # print("============================")
    # print(prompt)
    # print("============================")
    last_err = None
    content = ""
    for attempt in range(5):
        try:
            # 流式调用：添加 stream=True 和 stream_options
            stream = client.chat.completions.create(
                model=LLM_CONFIG["model"],
                messages=[{"role": "user", "content": prompt}],
                stream=True,
                stream_options={"include_usage": True},
                temperature=0.1,
                max_tokens=16384,
                extra_body={"enable_thinking": False},  # 关闭 think
            )
            # 遍历流式响应，累积内容
            usage = None
            for chunk in stream:
                usage = getattr(chunk, "usage", None) or usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = choices[0].delta
                if delta.content:
                    content += delta.content
                    print(delta.content, end="", flush=True)  # 实时输出
            print()  # 换行
            break
        except Exception as e:
            last_err = e
            print("  attempt {}/5 failed: {}".format(attempt + 1, e))
            if attempt < 4:
                time.sleep(4)
    else:
        raise last_err
    # print("resp:")
    # print("============================")
    # print(resp)
    # print("============================")
    elapsed = time.time() - t0
    completion_tokens = getattr(usage, "completion_tokens", None) if usage else None
    print("  [{}] {:.1f}s {}t {}c".format(label, elapsed, completion_tokens, len(content)))
    return content
