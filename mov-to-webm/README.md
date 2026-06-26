# Видео конвертер с альфа-каналом (v5)

Конвертирует `.mov` и `.webm (VP9)` видео с прозрачностью в **WebM** формат.

## Режимы кодирования

| Режим | Скорость | Сжатие | Совместимость |
|-------|----------|--------|---------------|
| **VP9 CPU** | ★★★★ | хорошее | 100% (все браузеры) |
| **AV1 CPU** | ★★ | лучшее | Chrome/Firefox/Edge |
| **AV1 GPU ★** | ★★★★★ | лучшее | Chrome 94+ / Firefox 113+ / Edge 94+ |

---

## Требования

- **Python 3.11+** (встроенные библиотеки, без `pip install`)
- **ffmpeg** с нужной поддержкой (см. ниже)

### ffmpeg для GPU режима (RTX 4060/4070/4080/4090)

Нужна полная CUDA сборка с NVENC:

**Windows:** https://github.com/BtbN/FFmpeg-Builds/releases  
→ скачать `ffmpeg-master-latest-win64-gpl-shared.zip`  
→ распаковать → добавить папку `bin` в PATH

**macOS/Linux:** `brew install ffmpeg` или `sudo apt install ffmpeg`  
(GPU режим только на NVIDIA в Windows/Linux)

### ffmpeg для CPU режима

Любая стандартная сборка с https://ffmpeg.org/download.html

---

## Запуск

```bash
python converter.py
```

---

## GPU режим (AV1 GPU / RTX 4060+)

**Как работает технически:**

Т.к. `av1_nvenc` не поддерживает `yuva420p` напрямую (ограничение железа),
используется двухпроходный подход:

```
Pass 1: GPU (av1_nvenc) кодирует:
  • Цветной поток (yuv420p)    → temp_color.mp4  [очень быстро]
  • Альфа-маска (grayscale)    → temp_alpha.mp4  [очень быстро]

Pass 2: ffmpeg собирает dual-track WebM:
  • Трек 0: AV1 цвет
  • Трек 1: AV1 альфа (grayscale)
  → output_av1gpu.webm         [мгновенно]
```

**Совместимость выходного файла:**
- ✅ Chrome 94+
- ✅ Firefox 113+
- ✅ Edge 94+
- ❌ Safari (не поддерживает AV1)

**Настройки:**
- **CRF** (0–51): качество. Рекомендуется 28–38.
- **GPU пресет** (p1–p7): скорость кодирования.
  - p1 = максимальная скорость
  - p4 = баланс (рекомендуется)
  - p7 = максимальное качество (медленнее)

---

## CPU режимы

### VP9 CPU (libvpx-vp9)
- Самый быстрый CPU-режим
- 100% совместимость с браузерами для alpha WebM
- CRF 20 — хорошее качество

### AV1 CPU (libaom-av1)
- Лучшее сжатие, но медленный на слабых CPU
- CRF 35 — хорошее качество
- cpu-used 4 — рекомендуемый баланс скорость/качество

---

## CRF / Quality

| Кодек | Отличное | Хорошее | Среднее | Низкое |
|-------|----------|---------|---------|--------|
| VP9   | 0–15     | 16–25   | 26–35   | 36–63  |
| AV1 CPU | 0–20   | 21–38   | 39–50   | 51–63  |
| AV1 GPU | 0–18   | 19–35   | 36–43   | 44–51  |

---

## Логи

Папка `logs/` рядом со скриптом. Файл: `conversion_YYYY-MM-DD_HH-MM-SS.log`

Содержат: режим, команды ffmpeg, обнаружение альфа-канала, ошибки, сжатие.

---

## Технические команды

### VP9 CPU (единый поток с альфой)
```
ffmpeg -hwaccel cuda -i input
  -map 0:v:0
  -c:v libvpx-vp9 -pix_fmt yuva420p -auto-alt-ref 0
  -b:v 0 -crf 20 -row-mt 1 -threads 16
  -an output.webm
```

### AV1 GPU Pass 1 (раздельное кодирование)
```
ffmpeg -hwaccel cuda -i input
  -filter_complex "[0:v:0]split[c][a];[c]format=yuv420p[co];[a]alphaextract,format=yuv420p[ao]"
  -map [co] -c:v av1_nvenc -cq 35 -preset p4 temp_color.mp4
  -map [ao] -c:v av1_nvenc -cq 35 -preset p4 temp_alpha.mp4
```

### AV1 GPU Pass 2 (сборка dual-track WebM)
```
ffmpeg -i temp_color.mp4 -i temp_alpha.mp4
  -map 0:v:0 -map 1:v:0 -c copy output.webm
```
