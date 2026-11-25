"""语义 token 直转（SoVITS 变声）：把一段源语音的内容+节奏+语调趋势，用目标音色重新说出来。
流程：源语音 → CNHuBERT → SoVITS.extract_latent 得到语义 token（自带说话人的节奏与停顿）→ 配合识别文本的音素与参考音色，
由 SoVITS 解码成波形。绕过了自回归(GPT)模型——GPT 在不带参考文本时不会跟随参考语气，带参考文本时又不稳定。
"""
import numpy as np, torch, librosa


def vc(tts, src_wav_path, text, ref_path, speed=1.0, lang="zh"):
    """tts: TTS_infer_pack.TTS.TTS 实例（已加载目标音色权重）。返回 (sr, int16 波形)。"""
    if not (text or "").strip():
        raise ValueError("文本为空（没有识别到内容）")
    dev = tts.configs.device
    with torch.no_grad():
        y, _ = librosa.load(src_wav_path, sr=16000, mono=True)
        y = np.concatenate([y, np.zeros(int(16000 * 0.3), dtype=np.float32)])  # 与 _set_prompt_semantic 一致，防止尾部被截
        w = torch.from_numpy(y).float().to(dev).unsqueeze(0)
        hubert = tts.cnhuhbert_model.model(w)["last_hidden_state"].transpose(1, 2)
        codes = tts.vits_model.extract_latent(hubert)  # [1,1,T]
        tts.set_ref_audio(ref_path)  # 参考 3~10 秒；这里只取它的音色（频谱 + 说话人向量）
        phones, _bert, _norm = tts.text_preprocessor.get_phones_and_bert(text, lang, tts.configs.version)
        phones = torch.LongTensor(phones).unsqueeze(0).to(dev)
        spec, sv_emb = [], ([] if tts.is_v2pro else None)
        for s, audio_tensor in tts.prompt_cache["refer_spec"]:
            spec.append(s.to(dtype=tts.precision, device=dev))
            if tts.is_v2pro:
                sv_emb.append(tts.sv_model.compute_embedding3(audio_tensor))
        out = tts.vits_model.decode(codes.to(dev), phones, spec, speed=speed, sv_emb=sv_emb).detach()[0, 0].float().cpu().numpy()
    peak = float(np.abs(out).max())
    if peak > 1:
        out = out / peak
    return tts.configs.sampling_rate, (out * 32767).astype(np.int16)
