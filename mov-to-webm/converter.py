"""
Видео конвертер с альфа-каналом  v5
Поддерживает: MOV / WebM(VP9) → WebM · VP9(CPU) или AV1(CPU) или AV1(GPU/NVENC)

Режимы кодирования:
  VP9  CPU  — libvpx-vp9,  быстрее CPU-кодировщиков, 100% совместимость
  AV1  CPU  — libaom-av1,  лучшее сжатие, медленно на слабом CPU
  AV1  GPU  — av1_nvenc,   RTX 4060/4070/4080/4090, двухпроходный:
                             Pass 1: GPU кодирует цвет + альфа раздельно
                             Pass 2: ffmpeg собирает dual-track WebM
               Совместимость: Chrome 94+, Firefox 113+, Edge 94+
"""

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import subprocess
import threading
import os
import sys
import json
import logging
import datetime


# ─── Логирование ──────────────────────────────────────────────────────────────

def setup_logger(log_dir: str) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = os.path.join(log_dir, f"conversion_{ts}.log")

    logger = logging.getLogger("AlphaConverter")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(ch)

    logger.info(f"Лог-файл: {log_file}")
    return logger


# ─── Проверки ffmpeg ──────────────────────────────────────────────────────────

def find_ffmpeg() -> bool:
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def check_encoder(name: str, logger: logging.Logger) -> bool:
    try:
        r = subprocess.run(["ffmpeg", "-encoders"], capture_output=True, text=True, timeout=10)
        ok = name in r.stdout
        logger.info(f"{name}: {'найден ✓' if ok else 'не найден'}")
        return ok
    except Exception as e:
        logger.warning(f"Не удалось проверить {name}: {e}")
        return False


def check_hwaccel_decode(logger: logging.Logger) -> str | None:
    """Лучший доступный hwaccel для декодирования."""
    candidates = ["cuda", "d3d11va", "videotoolbox", "vaapi", "dxva2", "qsv"]
    try:
        r = subprocess.run(["ffmpeg", "-hwaccels"], capture_output=True, text=True, timeout=10)
        available = r.stdout.lower()
        for hw in candidates:
            if hw in available:
                logger.info(f"GPU декодирование: {hw} ✓")
                return hw
    except Exception as e:
        logger.warning(f"Не удалось определить hwaccel: {e}")
    logger.info("GPU декодирование: не найдено")
    return None


# ─── Анализ файла ─────────────────────────────────────────────────────────────

def get_video_info(path: str, logger: logging.Logger) -> dict:
    result = {
        "duration": None,
        "video_streams": [],
        "has_dual_alpha": False,
        "pix_fmt_alpha": False,
    }
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", path],
            capture_output=True, text=True, timeout=60
        )
        if r.returncode != 0:
            logger.warning(f"ffprobe ошибка: {r.stderr.strip()}")
            return result

        data = json.loads(r.stdout)
        dur = data.get("format", {}).get("duration")
        if dur:
            result["duration"] = float(dur)

        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                info = {
                    "codec":   s.get("codec_name", "unknown"),
                    "pix_fmt": s.get("pix_fmt", "unknown"),
                    "width":   s.get("width", 0),
                    "height":  s.get("height", 0),
                }
                if not result["duration"] and s.get("duration"):
                    result["duration"] = float(s["duration"])
                result["video_streams"].append(info)

        vs = result["video_streams"]
        if vs:
            pf = vs[0]["pix_fmt"]
            result["pix_fmt_alpha"] = any(
                a in pf for a in ["yuva", "rgba", "bgra", "argb", "abgr", "gbrap"]
            )
            result["has_dual_alpha"] = len(vs) >= 2

        for i, s in enumerate(vs):
            logger.info(f"Поток #{i}: {s['codec']} {s['pix_fmt']} {s['width']}x{s['height']}")

        is_vp9_webm = (path.lower().endswith(".webm")
                       and vs and vs[0]["codec"] == "vp9")
        if result["has_dual_alpha"]:
            logger.info("✓ Два видео-потока → alphamerge (v:0 цвет + v:1 маска)")
        elif result["pix_fmt_alpha"]:
            logger.info(f"✓ Альфа в pix_fmt: {vs[0]['pix_fmt']}")
        elif is_vp9_webm:
            logger.info("VP9 WebM: ffprobe не видит альфу явно (VP9 AlphaMode). "
                        "ffmpeg прочитает альфу автоматически из единого потока.")
        else:
            logger.warning(
                f"Альфа не обнаружена в метаданных (pix_fmt={vs[0]['pix_fmt'] if vs else '?'}). "
                "Для .mov ProRes 4444/Animation это нормально — кодируется внутри потока."
            )

        dur_s = f"{result['duration']:.2f}с" if result["duration"] else "неизвестна"
        logger.info(f"Длительность: {dur_s}")

    except Exception as e:
        logger.warning(f"Ошибка ffprobe: {e}")
    return result


# ─── Вспомогательные функции ──────────────────────────────────────────────────

def tiles_for_resolution(width: int, height: int) -> str:
    if width >= 3840 or height >= 2160:
        return "4x4"
    if width >= 1920 or height >= 1080:
        return "2x2"
    return "1x2"


def auto_threads() -> int:
    return min(os.cpu_count() or 4, 16)


def run_ffmpeg_with_progress(cmd, dur_us, progress_cb, offset=0, scale=100,
                              logger=None):
    """
    Запускает ffmpeg-команду, читает прогресс.
    offset/scale позволяют вписать в диапазон [offset, offset+scale].
    Возвращает (returncode, stderr_str).
    """
    if logger:
        logger.info(f"Команда:\n  {' '.join(cmd)}")
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1
    )
    for line in process.stdout:
        line = line.strip()
        if line.startswith("out_time_us="):
            try:
                t = int(line.split("=")[1])
                if dur_us and dur_us > 0:
                    pct = min(int(t / dur_us * scale) + offset, offset + scale - 1)
                    progress_cb(pct)
            except ValueError:
                pass
        elif line.startswith("progress=end"):
            progress_cb(offset + scale)
    _, stderr = process.communicate()
    return process.returncode, stderr


# ─── РЕЖИМ 1: CPU кодирование (VP9 / AV1) ────────────────────────────────────

def build_cpu_cmd(input_path, output_path, codec, crf, cpu_used, hwaccel, info):
    has_dual = info["has_dual_alpha"]
    vs = info["video_streams"]
    width  = vs[0]["width"]  if vs else 0
    height = vs[0]["height"] if vs else 0
    threads = auto_threads()

    cmd = ["ffmpeg", "-y"]
    if hwaccel:
        cmd += ["-hwaccel", hwaccel]
    cmd += ["-i", input_path]

    if has_dual:
        cmd += ["-filter_complex", "[0:v:0][0:v:1]alphamerge[out]", "-map", "[out]"]
    else:
        cmd += ["-map", "0:v:0"]

    if codec == "vp9":
        cmd += [
            "-c:v", "libvpx-vp9",
            "-pix_fmt", "yuva420p",
            "-auto-alt-ref", "0",
            "-b:v", "0",
            "-crf", str(crf),
            "-row-mt", "1",
            "-threads", str(threads),
        ]
    else:  # av1
        tiles = tiles_for_resolution(width, height)
        cmd += [
            "-c:v", "libaom-av1",
            "-pix_fmt", "yuva420p",
            "-b:v", "0",
            "-crf", str(crf),
            "-cpu-used", str(cpu_used),
            "-row-mt", "1",
            "-tiles", tiles,
            "-threads", str(threads),
        ]

    cmd += ["-an", "-progress", "pipe:1", "-nostats", "-loglevel", "error", output_path]
    return cmd


def convert_cpu(input_path, output_path, codec, crf, cpu_used, use_gpu_decode,
                logger, progress_cb, status_cb, done_cb, error_cb):

    hwaccel = check_hwaccel_decode(logger) if use_gpu_decode else None
    if use_gpu_decode and hwaccel:
        logger.info(f"GPU декодирование: {hwaccel}")
    else:
        logger.info("CPU декодирование")

    size_mb = os.path.getsize(input_path) / 1024 ** 2

    if not check_encoder("libaom-av1" if codec == "av1" else "libvpx-vp9", logger):
        error_cb(f"Кодек {'libaom-av1' if codec == 'av1' else 'libvpx-vp9'} не найден в ffmpeg.")
        return

    status_cb("Анализ файла...")
    info = get_video_info(input_path, logger)
    duration = info["duration"]
    dur_us = int(duration * 1_000_000) if duration else None

    status_cb(f"Кодирование {codec.upper()} (CPU)...")
    cmd = build_cpu_cmd(input_path, output_path, codec, crf, cpu_used, hwaccel, info)
    logger.info(f"Команда:\n  {' '.join(cmd)}")

    rc, stderr = run_ffmpeg_with_progress(cmd, dur_us, progress_cb, offset=0, scale=100)

    if stderr.strip():
        hw_kws = ["hwaccel", "hwdownload", "hwupload", "hwframe", "cuda",
                  "nvenc", "vaapi", "d3d11", "qsv", "videotoolbox",
                  "invalid argument", "reinitializing filters"]
        is_hw_err = hwaccel and any(k in stderr.lower() for k in hw_kws)
        for ln in stderr.strip().splitlines():
            logger.error(f"ffmpeg: {ln}")
        if is_hw_err:
            logger.warning("GPU декодирование вызвало ошибку — повторяем без GPU...")
            status_cb("Повтор без GPU декодирования...")
            cmd2 = build_cpu_cmd(input_path, output_path, codec, crf, cpu_used, None, info)
            logger.info(f"Повтор:\n  {' '.join(cmd2)}")
            rc, stderr2 = run_ffmpeg_with_progress(cmd2, dur_us, progress_cb)
            if stderr2.strip():
                for ln in stderr2.strip().splitlines():
                    logger.error(f"ffmpeg retry: {ln}")

    if rc != 0:
        error_cb(f"ffmpeg вернул ошибку (код {rc}).\nСм. лог-файл.")
        return

    _finish(input_path, output_path, size_mb, logger, progress_cb, done_cb, error_cb)


# ─── РЕЖИМ 2: GPU кодирование (av1_nvenc, трёхпроходный) ────────────────────

def _run_pass(cmd, dur_us, progress_cb, offset, scale, label, logger):
    """
    Запускает одну ffmpeg-команду с прогрессом.
    Возвращает (returncode, stderr_text).
    """
    logger.info(f"{label}:\n  {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1
    )
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_us="):
            try:
                t = int(line.split("=")[1])
                if dur_us and dur_us > 0:
                    pct = min(int(t / dur_us * scale) + offset, offset + scale - 1)
                    progress_cb(pct)
            except ValueError:
                pass
        elif line.startswith("progress=end"):
            progress_cb(offset + scale)
    _, stderr = proc.communicate()
    return proc.returncode, stderr


def convert_gpu_nvenc(input_path, output_path, cq, gpu_preset,
                      logger, progress_cb, status_cb, done_cb, error_cb):
    """
    GPU-кодирование через av1_nvenc (RTX 4060+).

    Три отдельных вызова ffmpeg для стабильности на Windows:
      Pass 1 (0–44%):  GPU кодирует цветной поток       → temp_color.mp4
      Pass 2 (45–89%): GPU кодирует альфа-маску (grayscale) → temp_alpha.mp4
      Pass 3 (90–100%): ffmpeg собирает dual-track WebM  → output.webm

    ВАЖНО: -hwaccel cuda НЕ используется при кодировании.
    GPU задействован только для КОДИРОВАНИЯ (av1_nvenc).
    Смешение hwaccel-decode + filter + multi-output в одной команде
    вызывает ACCESS VIOLATION на Windows → поэтому три отдельные команды.

    Совместимость: Chrome 94+, Firefox 113+, Edge 94+.
    """
    if not check_encoder("av1_nvenc", logger):
        error_cb(
            "av1_nvenc не найден!\n\n"
            "Требуется NVIDIA RTX 4060 / 4070 / 4080 / 4090\n"
            "и полная сборка ffmpeg с CUDA:\n"
            "https://github.com/BtbN/FFmpeg-Builds/releases\n\n"
            "Скачайте: ffmpeg-master-latest-win64-gpl-shared.zip\n"
            "Распакуйте → папку bin добавьте в PATH."
        )
        return

    out_dir = os.path.dirname(output_path) or "."
    base = os.path.splitext(os.path.basename(output_path))[0]
    temp_color = os.path.join(out_dir, f"_tmp_{base}_color.mp4")
    temp_alpha = os.path.join(out_dir, f"_tmp_{base}_alpha.mp4")

    size_mb = os.path.getsize(input_path) / 1024 ** 2
    logger.info("=" * 56)
    logger.info(f"РЕЖИМ: GPU (av1_nvenc)  CQ={cq}  preset={gpu_preset}")
    logger.info(f"Входной файл : {input_path}  ({size_mb:.1f} МБ)")
    logger.info(f"Выходной файл: {output_path}")

    status_cb("Анализ файла...")
    info = get_video_info(input_path, logger)
    duration = info["duration"]
    dur_us = int(duration * 1_000_000) if duration else None
    has_dual = info["has_dual_alpha"]

    # Общие параметры av1_nvenc (без -hwaccel, без hwdownload/hwupload)
    nvenc_flags = [
        "-c:v", "av1_nvenc",
        "-cq", str(cq),
        "-preset", gpu_preset,
        "-an",
    ]
    progress_flags = ["-progress", "pipe:1", "-nostats", "-loglevel", "error"]

    try:
        # ── Pass 1: GPU кодирует ЦВЕТНОЙ поток ───────────────────────────────
        # Без -hwaccel: CPU декодирует, GPU кодирует через av1_nvenc.
        # Для dual-stream: сначала alphamerge чтобы получить полный кадр,
        # затем format=yuv420p убирает альфа-плоскость для цветного потока.
        if has_dual:
            color_map = [
                "-filter_complex", "[0:v:0][0:v:1]alphamerge,format=yuv420p[co]",
                "-map", "[co]",
            ]
        else:
            color_map = [
                "-map", "0:v:0",
                "-vf", "format=yuv420p",
            ]

        cmd_color = (
            ["ffmpeg", "-y", "-i", input_path]
            + color_map
            + nvenc_flags
            + progress_flags
            + [temp_color]
        )

        status_cb("Pass 1/3: GPU кодирует цвет (av1_nvenc)...")
        rc1, stderr1 = _run_pass(cmd_color, dur_us, progress_cb,
                                  offset=0, scale=44, label="Pass 1 — цвет", logger=logger)

        if stderr1.strip():
            for ln in stderr1.strip().splitlines():
                logger.error(f"pass1: {ln}")

        if rc1 != 0:
            logger.error(f"GPU Pass 1 упал (код {rc1})")
            error_cb(
                f"GPU кодирование цвета вернуло ошибку (код {rc1}).\n\n"
                "Убедитесь что у вас:\n"
                "• NVIDIA RTX 4060 / 4070 / 4080 / 4090\n"
                "• ffmpeg-master-latest-win64-gpl-shared.zip\n"
                "  (НЕ ffmpeg.org — там нет av1_nvenc)\n\n"
                "Подробности в лог-файле."
            )
            return

        # ── Pass 2: GPU кодирует АЛЬФА-маску ─────────────────────────────────
        # alphaextract превращает альфа-плоскость в grayscale-видео.
        # Для dual-stream: v:1 уже является маской (grayscale).
        #
        # ВАЖНО для VP9 AlphaMode WebM (pix_fmt=yuv420p в ffprobe):
        #   ffmpeg декодирует VP9 AlphaMode как yuv420p по умолчанию
        #   (format negotiation не запрашивает alpha).
        #   Чтобы получить yuva420p, нужен явный format=yuva420p
        #   ПЕРЕД alphaextract — это заставляет декодер выдать альфу.
        if has_dual:
            alpha_map = [
                "-map", "0:v:1",
                "-vf", "format=yuv420p",
            ]
        else:
            # format=yuva420p  → принудительно запросить альфу из декодера
            # alphaextract     → превратить альфа-плоскость в grayscale
            # format=yuv420p   → убрать альфу (теперь grayscale готов для nvenc)
            alpha_map = [
                "-map", "0:v:0",
                "-vf", "format=yuva420p,alphaextract,format=yuv420p",
            ]

        cmd_alpha = (
            ["ffmpeg", "-y", "-i", input_path]
            + alpha_map
            + nvenc_flags
            + progress_flags
            + [temp_alpha]
        )

        status_cb("Pass 2/3: GPU кодирует альфа-маску (av1_nvenc)...")
        rc2, stderr2 = _run_pass(cmd_alpha, dur_us, progress_cb,
                                  offset=45, scale=44, label="Pass 2 — альфа", logger=logger)

        if stderr2.strip():
            for ln in stderr2.strip().splitlines():
                logger.error(f"pass2: {ln}")

        if rc2 != 0:
            logger.error(f"GPU Pass 2 упал (код {rc2})")
            error_cb(
                f"GPU кодирование альфа-маски вернуло ошибку (код {rc2}).\n\n"
                "Возможно входной файл не содержит альфа-канала,\n"
                "или используется неподдерживаемый формат.\n\n"
                "Подробности в лог-файле."
            )
            return

        if not os.path.isfile(temp_color) or not os.path.isfile(temp_alpha):
            logger.error("Temp-файлы не созданы!")
            error_cb("Temp-файлы после Pass 1/2 не найдены.\nСм. лог-файл.")
            return

        # ── Pass 3: Сборка dual-track WebM ────────────────────────────────────
        # Цветной трек (AV1) + Альфа-трек (AV1 grayscale) → WebM.
        # -c copy: просто переупаковываем, очень быстро.
        cmd_mux = [
            "ffmpeg", "-y",
            "-i", temp_color,
            "-i", temp_alpha,
            "-map", "0:v:0",
            "-map", "1:v:0",
            "-c", "copy",
            "-an",
            "-progress", "pipe:1", "-nostats", "-loglevel", "error",
            output_path,
        ]

        status_cb("Pass 3/3: Сборка dual-track WebM...")
        rc3, stderr3 = _run_pass(cmd_mux, dur_us, progress_cb,
                                  offset=90, scale=10, label="Pass 3 — mux", logger=logger)

        if stderr3.strip():
            for ln in stderr3.strip().splitlines():
                logger.error(f"pass3: {ln}")

        if rc3 != 0:
            logger.error(f"Pass 3 (mux) упал (код {rc3})")
            error_cb(f"Сборка WebM вернула ошибку (код {rc3}).\nСм. лог-файл.")
            return

        _finish(input_path, output_path, size_mb, logger, progress_cb, done_cb, error_cb)

    finally:
        for f in [temp_color, temp_alpha]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                    logger.debug(f"Удалён temp: {f}")
                except Exception as e:
                    logger.warning(f"Не удалось удалить {f}: {e}")


def _finish(input_path, output_path, in_mb, logger, progress_cb, done_cb, error_cb):
    """Общий финал: проверяем файл, считаем сжатие."""
    if not os.path.isfile(output_path):
        logger.error("Выходной файл не создан!")
        error_cb("Конвертация завершена, но файл не найден.\nСм. лог.")
        return
    out_mb = os.path.getsize(output_path) / 1024 ** 2
    ratio = (1 - out_mb / in_mb) * 100 if in_mb > 0 else 0
    logger.info(f"=== Готово! {out_mb:.1f} МБ (сжатие {ratio:.0f}%) ===")
    progress_cb(100)
    done_cb(output_path, out_mb, ratio)


# ─── Параметры кодеков ────────────────────────────────────────────────────────

CODEC_PARAMS = {
    "vp9": {
        "label": "VP9  CPU  — быстро, совместимость 100%",
        "crf_default": 20, "crf_min": 0, "crf_max": 63,
        "desc_crf": {(0, 15): ("отличное", "#a6e3a1"), (16, 25): ("хорошее", "#a6e3a1"),
                     (26, 35): ("среднее", "#f9e2af"), (36, 63): ("низкое", "#f38ba8")},
    },
    "av1_cpu": {
        "label": "AV1  CPU  — лучшее сжатие, медленно на слабом CPU",
        "crf_default": 35, "crf_min": 0, "crf_max": 63,
        "desc_crf": {(0, 20): ("отличное", "#a6e3a1"), (21, 38): ("хорошее", "#a6e3a1"),
                     (39, 50): ("среднее", "#f9e2af"), (51, 63): ("низкое", "#f38ba8")},
    },
    "av1_gpu": {
        "label": "AV1  GPU  — RTX 4060+, быстро ★  (Chrome/Firefox/Edge 94+)",
        "crf_default": 35, "crf_min": 0, "crf_max": 51,
        "desc_crf": {(0, 18): ("отличное", "#a6e3a1"), (19, 35): ("хорошее", "#a6e3a1"),
                     (36, 43): ("среднее", "#f9e2af"), (44, 51): ("низкое", "#f38ba8")},
    },
}

CPU_USED_LABELS = {
    0: "0 — максимальное качество (очень медленно)",
    2: "2 — высокое качество (медленно)",
    4: "4 — баланс (рекомендуется)",
    6: "6 — быстро",
    8: "8 — максимальная скорость",
}

GPU_PRESETS = ["p1", "p2", "p3", "p4", "p5", "p6", "p7"]
GPU_PRESET_LABELS = {
    "p1": "p1 — максимальная скорость",
    "p2": "p2 — очень быстро",
    "p3": "p3 — быстро",
    "p4": "p4 — баланс (рекомендуется)",
    "p5": "p5 — качество",
    "p6": "p6 — высокое качество",
    "p7": "p7 — максимальное качество (медленно)",
}


# ─── UI константы ─────────────────────────────────────────────────────────────

BG      = "#1e1e2e"
BG2     = "#181825"
SURFACE = "#313244"
FG      = "#cdd6f4"
FG_DIM  = "#a6adc8"
ACCENT  = "#89b4fa"
GREEN   = "#a6e3a1"
YELLOW  = "#f9e2af"
RED     = "#f38ba8"
PURPLE  = "#cba6f7"
BTN_BG  = "#89b4fa"
BTN_FG  = "#1e1e2e"
FONT    = ("Segoe UI", 10)
FONT_B  = ("Segoe UI", 10, "bold")
FONT_T  = ("Segoe UI", 13, "bold")
FONT_S  = ("Segoe UI", 9)
FONT_LOG= ("Consolas", 9)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Видео конвертер — Альфа-канал  v5")
        self.resizable(False, False)
        self.configure(bg=BG)

        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        self.logger = setup_logger(log_dir)
        self.logger.info("Приложение запущено")

        self.codec_var       = tk.StringVar(value="av1_gpu")
        self.input_var       = tk.StringVar()
        self.output_var      = tk.StringVar()
        self.crf_var         = tk.IntVar(value=35)
        self.cpu_used_var    = tk.IntVar(value=4)
        self.gpu_decode_var  = tk.BooleanVar(value=True)
        self.gpu_preset_var  = tk.StringVar(value="p4")

        self._build_ui()
        self._on_codec_change()
        self._check_ffmpeg()

    # ── Построение UI ─────────────────────────────────────────────────────────

    def _build_ui(self):
        PAD = 14

        tk.Label(self, text="Видео конвертер с альфа-каналом",
                 font=FONT_T, bg=BG, fg=ACCENT
                 ).grid(row=0, column=0, columnspan=3, pady=(PAD, 2), padx=PAD)
        tk.Label(self, text="MOV / WebM(VP9)  →  WebM · VP9·CPU  или  AV1·CPU  или  AV1·GPU",
                 font=FONT_S, bg=BG, fg=FG_DIM
                 ).grid(row=1, column=0, columnspan=3, pady=(0, 8), padx=PAD)

        # ── Кодек ─────────────────────────────────────────────────────────────
        cf = tk.LabelFrame(self, text=" Режим кодирования ", font=FONT_B,
                           bg=BG, fg=FG, bd=1, relief="groove")
        cf.grid(row=2, column=0, columnspan=3, sticky="ew",
                padx=PAD, pady=(0, 6), ipadx=8, ipady=4)

        for val, p in CODEC_PARAMS.items():
            color = PURPLE if val == "av1_gpu" else FG
            tk.Radiobutton(
                cf, text=p["label"],
                variable=self.codec_var, value=val,
                font=FONT, bg=BG, fg=color, selectcolor=SURFACE,
                activebackground=BG, command=self._on_codec_change
            ).pack(anchor="w", padx=6, pady=2)

        # Пояснение GPU режима
        self.gpu_note = tk.Label(
            cf,
            text="  ★ GPU режим: двухпроходный av1_nvenc · требует RTX 4060/4070/4080/4090\n"
                 "    Выходной dual-track WebM: Chrome 94+, Firefox 113+, Edge 94+ воспроизводят альфу",
            font=FONT_S, bg=BG, fg=PURPLE, justify="left"
        )
        self.gpu_note.pack(anchor="w", padx=6, pady=(0, 4))

        # ── GPU декодирование (отдельно от кодирования) ───────────────────────
        gf = tk.LabelFrame(self, text=" GPU декодирование ", font=FONT_B,
                           bg=BG, fg=FG, bd=1, relief="groove")
        gf.grid(row=3, column=0, columnspan=3, sticky="ew",
                padx=PAD, pady=(0, 6), ipadx=8, ipady=2)
        tk.Checkbutton(
            gf, text="Использовать GPU для декодирования входного файла (рекомендуется)",
            variable=self.gpu_decode_var,
            font=FONT, bg=BG, fg=GREEN, selectcolor=SURFACE,
            activebackground=BG
        ).pack(anchor="w", padx=6, pady=2)

        # ── Входной файл ──────────────────────────────────────────────────────
        tk.Label(self, text="Входной файл:", font=FONT_B, bg=BG, fg=FG
                 ).grid(row=4, column=0, sticky="w", padx=PAD, pady=4)
        tk.Entry(self, textvariable=self.input_var, width=44,
                 bg=SURFACE, fg=FG, insertbackground=FG, relief="flat", font=FONT
                 ).grid(row=4, column=1, padx=4, pady=4)
        tk.Button(self, text="Обзор...", font=FONT,
                  bg="#585b70", fg=FG, relief="flat", cursor="hand2",
                  command=self._browse_input
                  ).grid(row=4, column=2, padx=(0, PAD), pady=4)

        self.input_hint = tk.Label(self, text="", font=FONT_S, bg=BG, fg=FG_DIM)
        self.input_hint.grid(row=5, column=1, sticky="w", padx=4)

        # ── Выходной файл ─────────────────────────────────────────────────────
        tk.Label(self, text="Выходной файл:", font=FONT_B, bg=BG, fg=FG
                 ).grid(row=6, column=0, sticky="w", padx=PAD, pady=4)
        tk.Entry(self, textvariable=self.output_var, width=44,
                 bg=SURFACE, fg=FG, insertbackground=FG, relief="flat", font=FONT
                 ).grid(row=6, column=1, padx=4, pady=4)
        tk.Button(self, text="Обзор...", font=FONT,
                  bg="#585b70", fg=FG, relief="flat", cursor="hand2",
                  command=self._browse_output
                  ).grid(row=6, column=2, padx=(0, PAD), pady=4)

        # ── Качество CRF ──────────────────────────────────────────────────────
        tk.Label(self, text="Качество (CRF):", font=FONT_B, bg=BG, fg=FG
                 ).grid(row=7, column=0, sticky="w", padx=PAD, pady=4)
        crf_f = tk.Frame(self, bg=BG)
        crf_f.grid(row=7, column=1, sticky="w", padx=4, pady=4)
        self.crf_slider = tk.Scale(crf_f, from_=0, to=63, orient="horizontal",
                                   variable=self.crf_var, length=210,
                                   bg=BG, fg=FG, troughcolor=SURFACE,
                                   highlightthickness=0, bd=0,
                                   command=self._update_crf_label)
        self.crf_slider.pack(side="left")
        self.crf_label = tk.Label(crf_f, text="", width=18,
                                  font=FONT, bg=BG, fg=GREEN)
        self.crf_label.pack(side="left", padx=6)

        # ── CPU: скорость AV1 ─────────────────────────────────────────────────
        self.cpu_row_lbl = tk.Label(self, text="Скорость AV1:", font=FONT_B, bg=BG, fg=FG)
        self.cpu_row_lbl.grid(row=8, column=0, sticky="w", padx=PAD, pady=4)
        cpu_f = tk.Frame(self, bg=BG)
        cpu_f.grid(row=8, column=1, sticky="w", padx=4, pady=4)
        self.cpu_slider = tk.Scale(cpu_f, from_=0, to=8, orient="horizontal",
                                   variable=self.cpu_used_var, length=140,
                                   bg=BG, fg=FG, troughcolor=SURFACE,
                                   highlightthickness=0, bd=0,
                                   command=self._update_cpu_label)
        self.cpu_slider.pack(side="left")
        self.cpu_label = tk.Label(cpu_f, text="", width=34,
                                  font=FONT_S, bg=BG, fg=FG_DIM)
        self.cpu_label.pack(side="left", padx=4)

        # ── GPU: пресет av1_nvenc ─────────────────────────────────────────────
        self.gpu_row_lbl = tk.Label(self, text="GPU пресет:", font=FONT_B, bg=BG, fg=PURPLE)
        self.gpu_row_lbl.grid(row=9, column=0, sticky="w", padx=PAD, pady=4)
        gpu_f = tk.Frame(self, bg=BG)
        gpu_f.grid(row=9, column=1, sticky="w", padx=4, pady=4)
        self.gpu_preset_combo = ttk.Combobox(
            gpu_f, textvariable=self.gpu_preset_var,
            values=GPU_PRESETS, state="readonly", width=8, font=FONT
        )
        self.gpu_preset_combo.pack(side="left")
        self.gpu_preset_lbl = tk.Label(gpu_f, text="", width=32,
                                       font=FONT_S, bg=BG, fg=PURPLE)
        self.gpu_preset_lbl.pack(side="left", padx=6)
        self.gpu_preset_var.trace_add("write", self._update_preset_label)

        # ── Прогресс ──────────────────────────────────────────────────────────
        tk.Label(self, text="Прогресс:", font=FONT_B, bg=BG, fg=FG
                 ).grid(row=10, column=0, sticky="w", padx=PAD, pady=(12, 4))
        self.prog_canvas = tk.Canvas(self, width=455, height=24, bg=SURFACE,
                                     highlightthickness=0, relief="flat")
        self.prog_canvas.grid(row=10, column=1, columnspan=2, padx=4, pady=(12, 4))
        self._bar = self.prog_canvas.create_rectangle(0, 0, 0, 24, fill=ACCENT, outline="")
        self._pct = self.prog_canvas.create_text(228, 12, text="0%",
                                                  fill=BG, font=FONT_B)

        # ── Статус ────────────────────────────────────────────────────────────
        self.status_var = tk.StringVar(value="Готов к работе")
        tk.Label(self, textvariable=self.status_var, font=FONT, bg=BG, fg=GREEN
                 ).grid(row=11, column=0, columnspan=3, pady=4)

        # ── Кнопка ────────────────────────────────────────────────────────────
        self.start_btn = tk.Button(
            self, text="▶  Начать конвертацию",
            font=FONT_B, bg=BTN_BG, fg=BTN_FG,
            activebackground="#74c7ec", relief="flat",
            cursor="hand2", padx=18, pady=8,
            command=self._start
        )
        self.start_btn.grid(row=12, column=0, columnspan=3, pady=(8, 4))

        # ── Лог ───────────────────────────────────────────────────────────────
        tk.Label(self, text="Лог:", font=FONT_B, bg=BG, fg=FG
                 ).grid(row=13, column=0, sticky="w", padx=PAD, pady=(10, 2))
        self.log_text = scrolledtext.ScrolledText(
            self, width=70, height=12,
            bg=BG2, fg=FG_DIM,
            insertbackground=FG, font=FONT_LOG,
            relief="flat", state="disabled"
        )
        self.log_text.grid(row=14, column=0, columnspan=3, padx=PAD, pady=(0, PAD))
        self._setup_log_handler()

    def _setup_log_handler(self):
        widget = self.log_text

        class UIHandler(logging.Handler):
            def emit(self_, record):
                msg = self_.format(record)
                lvl = record.levelname
                color = {"ERROR": RED, "WARNING": YELLOW}.get(lvl, FG_DIM)

                def append():
                    widget.configure(state="normal")
                    widget.insert("end", msg + "\n", lvl)
                    widget.tag_configure(lvl, foreground=color)
                    widget.see("end")
                    widget.configure(state="disabled")
                try:
                    widget.after(0, append)
                except Exception:
                    pass

        h = UIHandler()
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                         datefmt="%H:%M:%S"))
        self.logger.addHandler(h)

    # ── Реакция на смену кодека ───────────────────────────────────────────────

    def _on_codec_change(self, *_):
        codec = self.codec_var.get()
        p = CODEC_PARAMS[codec]

        # CRF
        self.crf_var.set(p["crf_default"])
        self.crf_slider.configure(to=p["crf_max"])
        self._update_crf_label()

        # CPU/GPU строки
        is_gpu = (codec == "av1_gpu")
        is_av1_cpu = (codec == "av1_cpu")

        if is_av1_cpu:
            self.cpu_row_lbl.grid()
            self.cpu_slider.master.grid()
            self._update_cpu_label()
        else:
            self.cpu_row_lbl.grid_remove()
            self.cpu_slider.master.grid_remove()

        if is_gpu:
            self.gpu_row_lbl.grid()
            self.gpu_preset_combo.master.grid()
            self._update_preset_label()
        else:
            self.gpu_row_lbl.grid_remove()
            self.gpu_preset_combo.master.grid_remove()

        # Подсказка входного формата
        hint = "Принимает: .mov  или  .webm VP9 (с альфа-каналом)"
        self.input_hint.configure(text=hint)

        # Авто-имя выходного файла
        inp = self.input_var.get()
        if inp:
            base = os.path.splitext(inp)[0]
            suffix = {"vp9": "_vp9", "av1_cpu": "_av1", "av1_gpu": "_av1gpu"}[codec]
            self.output_var.set(base + suffix + ".webm")

    # ── Слайдеры ──────────────────────────────────────────────────────────────

    def _crf_desc(self, v):
        for (lo, hi), (desc, color) in CODEC_PARAMS[self.codec_var.get()]["desc_crf"].items():
            if lo <= v <= hi:
                return desc, color
        return "среднее", YELLOW

    def _update_crf_label(self, *_):
        v = self.crf_var.get()
        desc, color = self._crf_desc(v)
        self.crf_label.configure(text=f"{v}  ({desc})", fg=color)

    def _update_cpu_label(self, *_):
        v = self.cpu_used_var.get()
        closest = min(CPU_USED_LABELS, key=lambda k: abs(k - v))
        self.cpu_label.configure(text=CPU_USED_LABELS[closest])

    def _update_preset_label(self, *_):
        p = self.gpu_preset_var.get()
        self.gpu_preset_lbl.configure(text=GPU_PRESET_LABELS.get(p, ""))

    # ── Обзор файлов ──────────────────────────────────────────────────────────

    def _browse_input(self):
        types = [("Видео", "*.mov *.webm"),
                 ("MOV", "*.mov"), ("WebM", "*.webm"),
                 ("Все файлы", "*.*")]
        path = filedialog.askopenfilename(title="Выберите входной файл", filetypes=types)
        if not path:
            return
        self.input_var.set(path)
        sz = os.path.getsize(path) / 1024 ** 2
        self.logger.info(f"Входной файл: {path}  ({sz:.1f} МБ)")
        codec = self.codec_var.get()
        suffix = {"vp9": "_vp9", "av1_cpu": "_av1", "av1_gpu": "_av1gpu"}[codec]
        base = os.path.splitext(path)[0]
        self.output_var.set(base + suffix + ".webm")

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            title="Сохранить как...",
            defaultextension=".webm",
            filetypes=[("WebM Video", "*.webm"), ("Все файлы", "*.*")]
        )
        if path:
            self.output_var.set(path)
            self.logger.info(f"Выходной файл: {path}")

    # ── Проверка ffmpeg ───────────────────────────────────────────────────────

    def _check_ffmpeg(self):
        if find_ffmpeg():
            self.logger.info(f"ffmpeg найден ✓  (CPU ядер: {auto_threads()})")
            # Проверяем av1_nvenc
            check_encoder("av1_nvenc", self.logger)
        else:
            self.logger.error("ffmpeg НЕ найден!")
            self.status_var.set("⚠ ffmpeg не найден!")
            messagebox.showwarning(
                "ffmpeg не найден",
                "ffmpeg не найден в PATH.\n\n"
                "Для GPU режима (av1_nvenc) нужна полная CUDA сборка:\n"
                "https://github.com/BtbN/FFmpeg-Builds/releases\n"
                "→ ffmpeg-master-latest-win64-gpl-shared.zip\n\n"
                "Для CPU режима: https://ffmpeg.org/download.html"
            )

    # ── Прогресс ──────────────────────────────────────────────────────────────

    def _set_progress(self, pct: int):
        w = int(455 * pct / 100)
        self.prog_canvas.coords(self._bar, 0, 0, w, 24)
        self.prog_canvas.itemconfigure(self._pct, text=f"{pct}%")

    def _update_progress(self, pct: int):
        self.after(0, lambda: self._set_progress(pct))

    def _update_status(self, text: str):
        self.after(0, lambda: self.status_var.set(text))

    # ── Колбэки ───────────────────────────────────────────────────────────────

    def _on_done(self, out: str, size_mb: float, ratio: float):
        def _ui():
            self._set_progress(100)
            self.status_var.set("✅  Видео готово!")
            self.start_btn.configure(state="normal")
            codec = self.codec_var.get()
            note = ("\n\n⚠ GPU режим: воспроизведите в Chrome/Firefox/Edge (94+)\n"
                    "   для проверки прозрачности."
                    if codec == "av1_gpu" else "")
            ratio_str = f"\nСжатие: {ratio:.0f}%" if ratio != 0 else ""
            messagebox.showinfo(
                "Готово!",
                f"✅  Видео готово!\n\nФайл: {out}\nРазмер: {size_mb:.1f} МБ"
                f"{ratio_str}{note}"
            )
        self.after(0, _ui)

    def _on_error(self, message: str):
        def _ui():
            self.status_var.set("❌  Ошибка!")
            self.start_btn.configure(state="normal")
            messagebox.showerror("Ошибка конвертации", message)
        self.after(0, _ui)

    # ── Запуск ────────────────────────────────────────────────────────────────

    def _start(self):
        inp = self.input_var.get().strip()
        out = self.output_var.get().strip()
        codec = self.codec_var.get()

        if not inp:
            messagebox.showwarning("Нет файла", "Укажите входной файл.")
            return
        if not out:
            messagebox.showwarning("Нет пути", "Укажите путь выходного файла.")
            return
        if not out.lower().endswith(".webm"):
            messagebox.showwarning("Расширение", "Выходной файл должен иметь расширение .webm")
            return

        out_dir = os.path.dirname(out)
        if out_dir:
            try:
                os.makedirs(out_dir, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Ошибка", f"Не удалось создать папку:\n{e}")
                return

        self.start_btn.configure(state="disabled")
        self._set_progress(0)
        self.status_var.set("Подготовка...")

        self.logger.info("=" * 56)
        self.logger.info(f"Режим: {codec.upper()}")

        if codec == "av1_gpu":
            threading.Thread(
                target=convert_gpu_nvenc,
                args=(
                    inp, out,
                    self.crf_var.get(),
                    self.gpu_preset_var.get(),
                    self.logger,
                    self._update_progress,
                    self._update_status,
                    self._on_done,
                    self._on_error,
                ),
                daemon=True
            ).start()
        else:
            # CPU режим (VP9 или AV1 CPU)
            actual_codec = "vp9" if codec == "vp9" else "av1"
            threading.Thread(
                target=convert_cpu,
                args=(
                    inp, out,
                    actual_codec,
                    self.crf_var.get(),
                    self.cpu_used_var.get(),
                    self.gpu_decode_var.get(),
                    self.logger,
                    self._update_progress,
                    self._update_status,
                    self._on_done,
                    self._on_error,
                ),
                daemon=True
            ).start()


# ─── Точка входа ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
