# 本地预置模型权重目录

此目录用于存放**本地加载**的模型权重，当前用于 Cross-Encoder 重排模型（`retrieval.rerank_model`）。

## 用途

配置页「模型」tab 的「重排模型 (Rerank)」块里，「模型路径/API」填本目录
（`models`，或任意绝对路径如 `/mydata/models`），「模型ID」填目录下的子目录名：

- 每个一级子目录视为一个模型，目录名即模型ID —— 界面上不显示路径前缀；
- 填好「模型路径/API」后点「获取模型」，会列出该目录下全部可用模型，点一个即填入「模型ID」；
- 保存后推理时拼成 `<模型路径>/<模型ID>` 直接本地加载，**不联网、不临时下载**。

## 另一种填法：远程重排服务

「模型路径/API」是**一栏两义**：除上面的本机权重目录外，还可以填一台远程重排服务的
地址（`http://host:8080` 或 `http://host:8080/v1`）。此时：

- 精排不再本地加载，而是把这一批 query-doc POST 给 `{地址}/rerank`
  （Cohere / Jina / vLLM / TEI 一致的契约：`{"model", "query", "documents","top_n"}`
  → `{"results":[{"index","relevance_score"}]}`）；
- 「模型ID」仍必填：按服务认的名字填（如 `bge-reranker-v2-m3`），它只进请求体、不进路径；
- 「测试模型」会真发一次最小请求，地址写错 / 服务没起 / 不是 rerank 接口都会当场报错。

本目录只对上面第一种填法有意义；第二种填法的权重在远端机器上。

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
