import argparse
from dataclasses import dataclass
from humanize import naturalsize
import os
from PIL import Image
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from rich.console import Console
from rich.text import Text

LATEX_INJECT = r"""\AtBeginDocument{
\makeatletter
\newsavebox{\mytempbox}

\NewCommandCopy{\origadjincludegraphics}{\adjincludegraphics}
\renewcommand{\adjincludegraphics}[2][]{\sbox{\mytempbox}{\origadjincludegraphics[#1]{#2}}\typeout{^^JIMAGE-INFO:  File=#2, Width=\the\wd\mytempbox, Height=\the\ht\mytempbox^^J}\usebox{\mytempbox}}

\NewCommandCopy{\origincludegraphics}{\includegraphics}
\renewcommand{\includegraphics}[2][]{\sbox{\mytempbox}{\origincludegraphics[#1]{#2}}\typeout{^^JIMAGE-INFO:  File=#2, Width=\the\wd\mytempbox, Height=\the\ht\mytempbox^^J}\usebox{\mytempbox}}

\makeatother
}"""

PT_PER_INCH = 72.27  # TeX point conversion (1 inch = 72.27 pt, approx.)

console = Console()


@dataclass
class ImageInfo:
    filename: str
    width_pt: float
    height_pt: float


def main(input_file: str | Path, output: str | Path | None, target_dpi: int):
    input_file = Path(input_file)
    if output is None:
        output = input_file.parent.with_suffix(".arxiv.tar")
    output = Path(output)

    if output.suffix == ".tar":
        with tempfile.TemporaryDirectory() as tmp_output:
            tmp_output = Path(tmp_output)
            arxivit(input_file, tmp_output, target_dpi)
            shutil.make_archive(str(output.with_suffix("")), "tar", tmp_output)
    else:
        os.makedirs(output)  # fail early if it already exists
        arxivit(input_file, output, target_dpi)

    console.print(f"🎉 Done! Output saved to {output}")


def arxivit(input_file: Path, output_dir: Path, target_dpi: int):
    compile_dir = Path(tempfile.mkdtemp())
    console.print(f"🔨 Compiling LaTeX… ({compile_dir})")
    stdout, deps_file = compile_latex(input_file, compile_dir)

    console.print("📜 Parsing compile log…")
    deps, bbl_file, image_infos = parse_compile_log(stdout, deps_file)
    if bbl_file:
        deps.append(
            bbl_file if bbl_file.is_absolute() else input_file.parent / bbl_file
        )
    else:
        console.log("Warning: No bbl file found.", style="yellow")

    def merge_image_infos(image_infos: list[ImageInfo]) -> dict[str, ImageInfo]:
        d: dict[str, ImageInfo] = {}
        for info in image_infos:
            key = info.filename.lower()  # latex allows case-insensitive filenames
            if key in d:
                if max(info.width_pt, info.height_pt) > max(
                    d[key].width_pt, d[key].height_pt
                ):
                    console.log(
                        f"Warning: Image included more than once: {info.filename}",
                        style="yellow",
                    )
                    d[key] = info
            else:
                d[key] = info
        return d

    image_infos = merge_image_infos(image_infos)

    deps = [dep for dep in deps if dep.suffix != ".aux"]
    console.print("📦 Processing dependencies…")
    for dep in deps:
        image_info = None
        for k in [
            str(dep).lower(),
            str(dep.with_suffix("")).lower(),
        ]:  # TODO: make this more robust. handle \graphicspath, etc.
            if k in image_infos:
                image_info = image_infos[k]
                break
        if dep.is_absolute():
            result, old_size, new_size = process_dependency(
                dep, output_dir / dep.name, image_info, target_dpi
            )
        else:
            dst = output_dir / dep
            if not dst.resolve().is_relative_to(output_dir.resolve()):
                # will probably never happen, but just in case
                raise ValueError(f"Dependency {dep} resolves outside of output_dir")
            dst.parent.mkdir(parents=True, exist_ok=True)
            result, old_size, new_size = process_dependency(
                input_file.parent / dep, dst, image_info, target_dpi
            )
        console.print(
            Text(f"   - {str(dep)}")
            + Text(f"  [{result}]" if result else "", style="green")
            + Text(
                f" => {naturalsize(new_size)}",
                style="dim",
            )
            + Text(
                f" {int((new_size / old_size) * 100)}%",
                style="blue bold",
            )
        )


def process_dependency(
    dep: Path, dst: Path, image_info: ImageInfo | None, target_dpi: int
) -> tuple[str | None, int, int]:
    result = None
    match dep.suffix.lower():
        case ".tex":
            result = process_latex(dep, dst)
        case ".pdf":
            result = process_pdf(dep, dst, image_info, target_dpi)
        case ".png" | ".jpg" | ".jpeg":
            result = process_image(dep, dst, image_info, target_dpi, force_jpeg=True)
        case _:
            shutil.copy(dep, dst)
    return result, dep.stat().st_size, dst.stat().st_size


def process_latex(
    src: Path,
    dst: Path,
):
    command = ["latexpand", "--keep-includes", f"--output={dst}", src]
    subprocess.run(command, check=True, capture_output=True)
    return "strip comments"


def process_image(
    src: Path,
    dst: Path,
    image_info: ImageInfo | None,
    target_dpi: int,
    force_jpeg: bool = False,
    jpeg_quality: int = 95,
) -> str | None:
    result = None
    with Image.open(src) as im:
        if image_info:
            width_in = image_info.width_pt / PT_PER_INCH
            height_in = image_info.height_pt / PT_PER_INCH
            width_px = int(round(width_in * target_dpi))
            height_px = int(round(height_in * target_dpi))
            scale = max(width_px, height_px) / max(im.size)
        else:
            console.log(
                f"Warning: No image size found in LaTeX compile log: {src.name}",
                style="yellow",
            )
            scale = None

        if (
            scale and scale < 0.9
        ):  # avoid unnecessary re-encoding for minor size changes
            dpi = im.info.get("dpi", (72, 72))  # default if no dpi info
            new_size = tuple(int(s * scale) for s in im.size)
            new_dpi = tuple(
                (new_s / s) * dpi for new_s, s, dpi in zip(new_size, im.size, dpi)
            )
            result = f"{im.size[0]}×{im.size[1]} -> {new_size[0]}×{new_size[1]}"
            im_resized = im.resize(new_size, resample=Image.Resampling.LANCZOS)
            if force_jpeg:
                result += " JPEG"
                im_resized = im_resized.convert("RGB")

            im_resized.save(
                dst,
                "JPEG" if force_jpeg else None,
                dpi=new_dpi,
                quality=jpeg_quality,
            )
        else:
            if force_jpeg and im.format != "JPEG":
                result = "JPEG"
                im = im.convert("RGB")
                im.save(
                    dst,
                    "JPEG",
                    quality=jpeg_quality,
                )
            else:
                shutil.copy(src, dst)  # avoid re-enconding jpeg
        return result


def process_pdf(
    src: Path, dst: Path, image_info: ImageInfo | None, target_dpi: int
) -> str:
    # just use /prepress for now
    command = [
        "gs",
        "-o",
        dst,
        "-sDEVICE=pdfwrite",
        "-dPDFSETTINGS=/prepress",
        "-f",
        src,
    ]
    subprocess.run(command, check=True, capture_output=True)
    return "/prepress"


def compile_latex(input_file: Path, compile_dir: Path) -> tuple[str, Path]:
    deps_file = compile_dir / ".deps"
    command = [
        "latexmk",
        "-pdf",
        f"-auxdir={compile_dir}",
        f"-outdir={compile_dir}",
        "-deps",
        f"-deps-out={deps_file}",
        f"-usepretex={' '.join(LATEX_INJECT.splitlines())}",
        input_file,
    ]
    res = subprocess.run(
        command, cwd=input_file.parent, capture_output=True, check=True
    )
    return res.stdout.decode(), deps_file


def parse_compile_log(
    stdout: str, deps_file
) -> tuple[list[Path], Path | None, list[ImageInfo]]:
    def find_last_path(pattern, string) -> Path | None:
        matches = re.findall(pattern, string)
        if matches:
            return Path(matches[-1])
        return None

    with open(deps_file, "r") as f:
        deps = f.read().splitlines()
    deps = [Path(dep.strip().rstrip("\\")) for dep in deps if dep.startswith("    ")]
    deps = [dep for dep in deps if not dep.is_absolute()]

    bbl_file = find_last_path(r"Latexmk: Found input bbl file '(.+)'", stdout)

    image_infos = [
        ImageInfo(filename=n, width_pt=float(w), height_pt=float(h))
        for (n, w, h) in re.findall(
            r"IMAGE-INFO: File=(.+?), Width=([\d.]+)pt, Height=([\d.]+)pt",  # TODO: this currently also matches the injected latex code
            "".join(stdout.splitlines()),  # join to handle arbitrary line breaks
        )
    ]

    return deps, bbl_file, image_infos


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="arxiv-latex-graphics")
    parser.add_argument(
        "input_tex",
        type=str,
        help="Path to the input LaTeX file",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        help="Path to the output. Can be a dir or .tar file.",
    )
    parser.add_argument(
        "-d",
        "--dpi",
        type=int,
        default=300,
        help="Target DPI of the output_dir images",
    )
    args = parser.parse_args()

    main(
        input_file=args.input_tex,
        output=args.output,
        target_dpi=args.dpi,
    )
