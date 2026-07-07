#!/usr/bin/env python3
"""Render the initial-project architecture deck as a multi-page PDF.

The Markdown file next to this script is the content source of truth. The PDF is
rendered with Pillow so the deliverable does not depend on presentation-specific tooling.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


PAGE_W = 1920
PAGE_H = 1080
MARGIN_X = 92
SOURCE_PATH = Path(__file__).with_name("initial-project-architecture-deck.md")
OUTPUT_PATH = Path(__file__).with_name("initial-project-architecture-deck.pdf")
ROOT_DIR = Path(__file__).resolve().parents[2]
ASSET_DIR = ROOT_DIR / "website" / "static" / "img"

VISUALS = {
    1: ["banner.png"],
    3: ["architecture.png"],
    5: ["signal-0.png"],
    7: ["dashboard/config.png", "grafana_screenshot.png"],
    13: ["fleet-sim/pareto-frontier.png"],
}

CARD_SLIDES = {2, 4, 6, 8, 9, 10, 11, 12, 14, 15, 16}

MERMAID_SOURCES = {
    8: (ROOT_DIR / "docs" / "poc" / "03-strix-halo-runbook.md", 0),
    9: (ROOT_DIR / "docs" / "poc" / "07-client-server-topology.md", 0),
    10: (ROOT_DIR / "deploy" / "recipes" / "strix-halo-fleet-2box" / "docs" / "research-pipeline.md", 0),
    14: (ROOT_DIR / "docs" / "poc" / "08-topology-promotion-and-governance.md", 0),
}


@dataclass
class ParsedSlide:
    number: int
    title: str
    points: list[str]
    sources: list[str]


def rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))


def font_candidates(bold: bool) -> list[Path]:
    windows = Path("C:/Windows/Fonts")
    return [
        windows / ("msjhbd.ttc" if bold else "msjh.ttc"),
        windows / ("segoeuib.ttf" if bold else "segoeui.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]


def load_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    for candidate in font_candidates(bold):
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def text_width(draw: ImageDraw.ImageDraw, text: str, face: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), text, font=face)
    return box[2] - box[0]


def wrap_text(draw: ImageDraw.ImageDraw, text: str, face: ImageFont.ImageFont, max_width: int) -> list[str]:
    wrapped: list[str] = []
    for source in text.splitlines() or [""]:
        words = source.split()
        if not words:
            wrapped.append("")
            continue
        line = words[0]
        for word in words[1:]:
            candidate = f"{line} {word}"
            if text_width(draw, candidate, face) <= max_width:
                line = candidate
            else:
                wrapped.append(line)
                line = word
        wrapped.append(line)
    return wrapped


def draw_wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    max_width: int,
    face: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    *,
    line_gap: int = 8,
    max_lines: int | None = None,
) -> int:
    x, y = xy
    line_height = face.size + line_gap if hasattr(face, "size") else 30
    lines = wrap_text(draw, text, face, max_width)
    if max_lines is not None:
        lines = lines[:max_lines]
    for line in lines:
        draw.text((x, y), line, font=face, fill=fill)
        y += line_height
    return y


def clean_inline(text: str) -> str:
    text = text.strip().replace("`", "")
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    return text.replace("→", "->")


def parse_markdown(path: Path) -> list[ParsedSlide]:
    content = path.read_text(encoding="utf-8")
    matches = list(re.finditer(r"^## Slide (\d+)\. (.+)$", content, flags=re.MULTILINE))
    slides: list[ParsedSlide] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        number = int(match.group(1))
        title = clean_inline(match.group(2))
        body = content[start:end].splitlines()
        points: list[str] = []
        sources: list[str] = []
        mode = "points"
        for raw_line in body:
            line = raw_line.strip()
            if not line:
                continue
            if line.endswith(":") and not line.startswith("-"):
                label = line[:-1]
                mode = "sources" if label == "Source anchors" else "points"
                if label != "Source anchors":
                    points.append(f"{label}:")
                continue
            if line.startswith("Source anchors"):
                mode = "sources"
                continue
            if line.startswith(("Message:", "Customer takeaway:", "Caveat:")):
                points.append(clean_inline(line))
                mode = "points"
                continue
            if line.startswith("-"):
                item = clean_inline(line[1:])
                if mode == "sources":
                    sources.append(item)
                else:
                    points.append(item)
        slides.append(ParsedSlide(number=number, title=title, points=points, sources=sources))
    return slides


def extract_mermaid_block(path: Path, block_index: int) -> str:
    content = path.read_text(encoding="utf-8")
    blocks = re.findall(r"```mermaid\s*(.*?)```", content, flags=re.DOTALL)
    if block_index >= len(blocks):
        raise IndexError(f"{path} has {len(blocks)} Mermaid blocks, requested {block_index}")
    return blocks[block_index].strip() + "\n"


def mmdc_command(base_args: list[str]) -> list[str]:
    mmdc = shutil.which("mmdc") or shutil.which("mmdc.cmd") or shutil.which("mmdc.ps1")
    if not mmdc:
        raise FileNotFoundError("mmdc was not found on PATH")
    mmdc_path = Path(mmdc)
    if mmdc_path.suffix.lower() == ".ps1":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(mmdc_path), *base_args]
    return [str(mmdc_path), *base_args]


def render_mermaid_images(work_dir: Path) -> dict[int, Path]:
    rendered: dict[int, Path] = {}
    if not shutil.which("mmdc") and not shutil.which("mmdc.cmd") and not shutil.which("mmdc.ps1"):
        print("warning: mmdc not found; falling back to non-Mermaid visuals")
        return rendered

    puppeteer_config = work_dir / "puppeteer.json"
    puppeteer_config.write_text('{"args":["--no-sandbox","--disable-setuid-sandbox"]}\n', encoding="utf-8")
    for slide_number, (source_path, block_index) in MERMAID_SOURCES.items():
        mermaid_path = work_dir / f"slide{slide_number}.mmd"
        image_path = work_dir / f"slide{slide_number}.png"
        mermaid_path.write_text(extract_mermaid_block(source_path, block_index), encoding="utf-8")
        command = mmdc_command(
            [
                "-i",
                str(mermaid_path),
                "-o",
                str(image_path),
                "-b",
                "white",
                "-t",
                "default",
                "-p",
                str(puppeteer_config),
            ]
        )
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            message = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
            print(f"warning: failed to render Mermaid for slide {slide_number}: {message}")
            continue
        if image_path.exists():
            rendered[slide_number] = image_path
    return rendered


def place_image(page: Image.Image, path: Path, frame: tuple[int, int, int, int], *, padding: int = 0) -> None:
    if not path.exists():
        return
    image = Image.open(path).convert("RGBA")
    max_size = (frame[2] - frame[0] - padding * 2, frame[3] - frame[1] - padding * 2)
    image.thumbnail(max_size, Image.Resampling.LANCZOS)
    x = frame[0] + padding + (max_size[0] - image.width) // 2
    y = frame[1] + padding + (max_size[1] - image.height) // 2
    page.alpha_composite(image, (x, y))


def paste_visual(page: Image.Image, draw: ImageDraw.ImageDraw, slide_number: int, mermaid_images: dict[int, Path]) -> None:
    mermaid_image = mermaid_images.get(slide_number)
    if mermaid_image:
        frame = (900, 184, 1788, 836)
        draw.rounded_rectangle(frame, radius=28, fill=rgb("F7F9FB"), outline=rgb("A8B8C4"), width=3)
        place_image(page, mermaid_image, frame, padding=28)
        return

    visual_paths = VISUALS.get(slide_number, [])
    if not visual_paths:
        return
    if len(visual_paths) == 1:
        frame = (920, 214, 1762, 760)
        draw.rounded_rectangle(frame, radius=28, fill=rgb("0E1A20"), outline=rgb("21343E"), width=3)
        place_image(page, ASSET_DIR / visual_paths[0], frame, padding=24)
        return
    frames = ((900, 190, 1776, 480), (900, 530, 1776, 820))
    for path, frame in zip(visual_paths, frames):
        draw.rounded_rectangle(frame, radius=22, fill=rgb("0E1A20"), outline=rgb("21343E"), width=3)
        place_image(page, ASSET_DIR / path, frame, padding=18)


def draw_card_grid(draw: ImageDraw.ImageDraw, slide: ParsedSlide, mermaid_images: dict[int, Path]) -> None:
    if slide.number not in CARD_SLIDES or slide.number in mermaid_images:
        return
    key_points = [point for point in slide.points if not point.endswith(":")][:6]
    start_x = 920
    start_y = 214
    card_w = 390
    card_h = 128
    gap_x = 34
    gap_y = 30
    fills = ["13251A", "102E3D", "3E2D0B", "1B242A", "103820", "402019"]
    outlines = ["7AC70C", "35A2F4", "FDB515", "536A76", "4F7D26", "FF6A3D"]
    card_face = load_font(25, bold=True)
    small_face = load_font(18, bold=True)
    for index, point in enumerate(key_points):
        col = index % 2
        row = index // 2
        left = start_x + col * (card_w + gap_x)
        top = start_y + row * (card_h + gap_y)
        rect = (left, top, left + card_w, top + card_h)
        draw.rounded_rectangle(rect, radius=24, fill=rgb(fills[index % len(fills)]), outline=rgb(outlines[index % len(outlines)]), width=3)
        draw.text((left + 22, top + 16), f"{index + 1:02d}", font=small_face, fill=rgb(outlines[index % len(outlines)]))
        draw_wrapped(draw, point, (left + 70, top + 18), card_w - 94, card_face, rgb("F4F8FB"), line_gap=4, max_lines=3)


def draw_sources(draw: ImageDraw.ImageDraw, sources: list[str]) -> None:
    if not sources:
        return
    source_face = load_font(16)
    source_text = "Sources: " + "; ".join(sources[:4])
    if len(sources) > 4:
        source_text += "; ..."
    draw_wrapped(draw, source_text, (MARGIN_X, 990), 1600, source_face, rgb("7F929E"), line_gap=4, max_lines=2)


def render_slide(slide: ParsedSlide, mermaid_images: dict[int, Path]) -> Image.Image:
    page = Image.new("RGBA", (PAGE_W, PAGE_H), rgb("071014") + (255,))
    draw = ImageDraw.Draw(page)
    draw.rectangle((0, 0, 16, PAGE_H), fill=rgb("7AC70C"))

    label_face = load_font(19, bold=True)
    title_face = load_font(43, bold=True)
    point_face = load_font(26)
    section_face = load_font(18, bold=True)

    draw.text((MARGIN_X, 48), "vLLM Semantic Router | Initial Project", font=label_face, fill=rgb("7F929E"))
    draw.text((1690, 48), f"{slide.number:02d} / 16", font=label_face, fill=rgb("7F929E"))
    draw_wrapped(draw, slide.title, (MARGIN_X, 92), 1160, title_face, rgb("F4F8FB"), line_gap=8, max_lines=2)

    cursor_y = 218
    for point in slide.points[:9]:
        if point.endswith(":"):
            draw.text((MARGIN_X, cursor_y), point, font=section_face, fill=rgb("B7F06A"))
            cursor_y += 42
            continue
        draw.text((MARGIN_X, cursor_y + 7), "-", font=point_face, fill=rgb("7AC70C"))
        next_y = draw_wrapped(draw, point, (MARGIN_X + 36, cursor_y), 760, point_face, rgb("DFE8EE"), line_gap=9, max_lines=3)
        cursor_y = next_y + 22
        if cursor_y > 900:
            break

    paste_visual(page, draw, slide.number, mermaid_images)
    draw_card_grid(draw, slide, mermaid_images)
    draw_sources(draw, slide.sources)
    return page.convert("RGB")


def write_pdf(slides: list[ParsedSlide], output_path: Path, mermaid_images: dict[int, Path]) -> None:
    pages = [render_slide(slide, mermaid_images) for slide in slides]
    if not pages:
        raise RuntimeError("No slides found in Markdown source")
    if output_path.exists():
        output_path.unlink()
    first, *rest = pages
    first.save(output_path, "PDF", resolution=144.0, save_all=True, append_images=rest)


def main() -> None:
    slides = parse_markdown(SOURCE_PATH)
    with tempfile.TemporaryDirectory(prefix="vsr-deck-mermaid-") as temp_dir:
        mermaid_images = render_mermaid_images(Path(temp_dir))
        write_pdf(slides, OUTPUT_PATH, mermaid_images)
    print(f"Wrote {OUTPUT_PATH} ({len(slides)} pages)")


if __name__ == "__main__":
    main()
