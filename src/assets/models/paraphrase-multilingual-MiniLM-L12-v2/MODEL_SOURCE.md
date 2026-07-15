# Bundled multilingual embedding model

- FastEmbed model: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
- FastEmbed package: `0.8.0`
- FastEmbed ONNX source repository: `qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q`
- Source revision: `faf4aa4225822f3bc6376869cb1164e8e3feedd0`
- License reported by FastEmbed: Apache-2.0

The files in this directory are application assets. Runtime initialization uses
`specific_model_path` and `local_files_only=True`, so RightMeal never downloads
this model while the application is running.
