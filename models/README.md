# 本地预置模型权重目录

此目录用于存放**本地加载**的模型权重，当前用于 Cross-Encoder 重排模型（`retrieval.rerank_model`）。

## 用途

配置页「模型」tab 的「重排模型 (Rerank)」块默认从本目录扫描可用权重：

- 每个一级子目录视为一个模型，目录名即模型名；
- 点击「测试连接」会列出本目录下全部可用模型，点击即可选用；
- 选中后配置保存为相对路径（如 `models/bge-reranker-base`），推理时直接本地加载，
  **不联网、不临时下载**。

## 预置方法（离线环境）

在可联网机器上下载权重后，将整个模型目录拷贝到本目录：

```bash
# 联网机器：下载到本地（任选一种）
pip install "huggingface_hub[cli]"
hf download BAAI/bge-reranker-base --local-dir ./models/bge-reranker-base

# 或使用镜像
HF_ENDPOINT=https://hf-mirror.com hf download BAAI/bge-reranker-base \
  --local-dir ./models/bge-reranker-base
```

拷贝完成后目录结构示例：

```
models/
└── bge-reranker-base/
    ├── config.json
    ├── model.safetensors
    ├── tokenizer.json
    └── ...
```

## 选型建议

| 模型 | 参数量 | 内存(fp32) | 适用 |
|------|--------|-----------|------|
| `BAAI/bge-reranker-base` | ~0.3B | ~1GB | 无 GPU 的服务器（CPU 推理） |
| `BAAI/bge-reranker-v2-m3` | ~0.6B | ~2GB+ | 有 GPU 或内存充裕的机器 |

- 推理设备（CPU / GPU）在配置页「重排模型」块中选择，保存后热生效；
- 选择 GPU 但环境无 CUDA 时自动回退 CPU 并记录告警；
- 若本目录为空且填写的是 HuggingFace ID，运行时将尝试在线获取（离线环境会失败并
  降级为 LLM 重排）。
