# LM Studio per-model defaults

Example presets from one lab (LM Studio on a 12 GB GPU). The shipped example
config does not reference these models; keep them as a pattern for your own.

Copies of the per-model default-config files LM Studio reads when it loads a
model — by `lms load` *and* by JIT loading on the first API request. They
are what makes the served context 32k instead of LM Studio's 8k default
without anyone passing `--context-length`.

Installed location (file name = the model's indexed identifier + `.json`):

```
~/.lmstudio/.internal/user-concrete-model-default-config/
  LiquidAI/LFM2.5-2.6B-GGUF/LFM2.5-2.6B-Q8_0.gguf.json         <- lfm2.5-2.6b.json
  unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ2_XXS.gguf.json     <- qwen3.8-27b.json
```

Picked up on the next load, no daemon restart needed (verified 2026-08-16,
llmster 0.0.21-2). Values:

- both: `llm.load.contextLength` 32768, flash attention on
- qwen: KV cache q8_0 for K and V, 1 parallel slot — fp16 KV at 32k does
  not fit next to the 9.9 GB IQ2_XXS weights on the 12 GB RTX 4070
  (llama-server SIGABRT); q8_0 lands at ~10.4 GB.

`lms load` flags override these; JIT uses them as is.
