import argparse
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Callable

import pathspec
from humanize import naturalsize
from PIL import Image
from rich.console import Console
from rich.progress import track
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

console = Console(soft_wrap=True, log_time=False, highlight=False, markup=False)


@dataclass
class ImageInfo:
    filename: str
    width_pt: float
    height_pt: float


@dataclass
class SizeValue:
    value: int


@dataclass
class DpiValue(SizeValue):
    pass


@dataclass
class PxValue(SizeValue):
    pass


@dataclass
class ImageOptions:
    sizes: list[SizeValue]
    jpeg: bool
    jpeg_quality: int


class CliError(Exception):
    pass


def arxivit(
    input_file: Path,
    output_dir: Path,
    image_options_list: list[tuple[Callable[[Path], bool], ImageOptions]],
):
    input_file = input_file.resolve()
    compile_dir = Path(tempfile.mkdtemp())
    _log_file_text = Text(
        f" {compile_dir / input_file.with_suffix('.log').name}", style="dim"
    )
    with console.status(Text("Compiling LaTeX") + _log_file_text):
        stdout, deps_file = compile_latex(input_file, compile_dir)
    console.print(Text("🔨 Compiled LaTeX") + _log_file_text)

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
                        f"Info: Image included more than once: {info.filename}",
                        style="yellow",
                    )
                    d[key] = info
            else:
                d[key] = info
        return d

    image_infos = merge_image_infos(image_infos)
    console.print("📜 Parsed compile log")

    deps = [dep for dep in deps if dep.suffix != ".aux"]
    for dep in track(deps, console=console, description="📦 Processing dependencies"):
        image_options = None
        for match, im_opts in reversed(image_options_list):
            if match(dep):
                image_options = im_opts
                break
        image_info = None
        for k in [
            str(dep).lower(),
            str(dep.with_suffix("")).lower(),
        ]:  # TODO: make this more robust. handle \graphicspath, etc.
            if k in image_infos:
                image_info = image_infos[k]
                break
        if dep.is_absolute():
            status_before, status_after, old_size, new_size = process_dependency(
                dep,
                output_dir / dep.name,
                image_info,
                image_options,
            )
        else:
            dst = output_dir / dep
            if not dst.resolve().is_relative_to(output_dir.resolve()):
                # will probably never happen, but just in case
                raise CliError(
                    f"Dependency {dep} would be moved outside of output_dir to: {dst}."
                )
            dst.parent.mkdir(parents=True, exist_ok=True)
            status_before, status_after, old_size, new_size = process_dependency(
                input_file.parent / dep,
                dst,
                image_info,
                image_options,
            )
        console.print(
            Text(f"   - {str(dep)}  ")
            + Text(status_before if status_before else "", style="dim")
            + Text(" -> " if status_before and status_after else "", style="dim")
            + Text(status_after if status_after else "", style="green")
            + Text(
                f"  => {naturalsize(new_size)}",
                style="dim",
            )
            + Text(
                f" {int((new_size / old_size) * 100)}%",
                style="blue bold",
            )
        )


def process_dependency(
    dep: Path,
    dst: Path,
    image_info: ImageInfo | None,
    image_options: ImageOptions | None,
) -> tuple[str | None, str | None, int, int]:
    status_before = None
    status_after = None
    match dep.suffix.lower():
        case ".tex":
            status_after = process_latex(dep, dst)
        case ".pdf":
            status_after = process_pdf(dep, dst, image_info, image_options)
        case ".png" | ".jpg" | ".jpeg":
            status_before, status_after = process_image(
                dep, dst, image_info, image_options
            )
        case _:
            shutil.copy(dep, dst)
    return status_before, status_after, dep.stat().st_size, dst.stat().st_size


def process_latex(
    src: Path,
    dst: Path,
) -> str:
    command = ["latexpand", "--keep-includes", f"--output={dst}", src]
    subprocess.run(command, check=True, capture_output=True)
    return "strip comments"


def process_image(
    src: Path,
    dst: Path,
    image_info: ImageInfo | None,
    image_options: ImageOptions | None,
) -> tuple[str, str | None]:
    with Image.open(src) as im:
        status_before = f"{im.size[0]}×{im.size[1]} {im.format}"
        status_after = None
        if image_options:

            def compute_scale(size: SizeValue) -> float | None:
                match size:
                    case PxValue(px):
                        return px / max(im.size)
                    case DpiValue(dpi):
                        if image_info:
                            max_in = (
                                max(image_info.width_pt, image_info.height_pt)
                                / PT_PER_INCH
                            )
                            max_px = int(round(max_in * dpi))
                            return max_px / max(im.size)
                        else:
                            console.log(
                                "Warning: No image size found in LaTeX compile log.",
                                style="yellow",
                            )
                            return None
                    case _:
                        raise ValueError("Invalid image size option")

            scales = [compute_scale(size) for size in image_options.sizes]
            if None not in scales and len(scales) > 0:
                scale = max(scales)  # type: ignore
            else:
                scale = None

            if (
                scale and scale < 0.9  # TODO: make this threshold configurable
            ):  # avoid unnecessary re-encoding for minor size changes
                dpi = im.info.get("dpi", (72, 72))  # default if no dpi info
                new_size = tuple(int(s * scale) for s in im.size)
                new_dpi = tuple(
                    (new_s / s) * dpi for new_s, s, dpi in zip(new_size, im.size, dpi)
                )
                status_after = f"{new_size[0]}×{new_size[1]}"
                im_resized = im.resize(new_size, resample=Image.Resampling.LANCZOS)
                if image_options.jpeg and im.format != "JPEG":
                    try:
                        im_resized.save(
                            dst,
                            "JPEG",
                            dpi=new_dpi,
                            quality=image_options.jpeg_quality,
                        )
                        status_after += f" JPEG@{image_options.jpeg_quality}"
                    except OSError as e:  # color mode not supported
                        console.log(f"Error: {str(e)}", style="red")
                        im_resized.save(
                            dst,
                            im.format,
                            dpi=new_dpi,
                        )
                else:
                    im_resized.save(
                        dst,
                        im.format,
                        dpi=new_dpi,
                        quality=image_options.jpeg_quality,  # only relevant for JPEG
                    )
            else:
                if image_options.jpeg and im.format != "JPEG":
                    try:
                        im.save(
                            dst,
                            "JPEG",
                            quality=image_options.jpeg_quality,
                        )
                        status_after = f"JPEG@{image_options.jpeg_quality}"
                    except OSError as e:  # color mode not supported
                        console.log(f"Error: {str(e)}", style="red")
                        shutil.copy(src, dst)
                else:
                    shutil.copy(src, dst)  # avoid re-enconding jpeg
        else:
            shutil.copy(src, dst)
        return status_before, status_after


def process_pdf(
    src: Path,
    dst: Path,
    image_info: ImageInfo | None,
    image_options: ImageOptions | None,
) -> str | None:
    if not image_options or not image_options.sizes:
        shutil.copy(src, dst)
        return None

    console.log(
        "Warning: PDFs are currently always processed with /prepress; the provided dpi and px values are ignored.",
        style="yellow",
    )

    # just use /prepress for now, which is 300dpi in src's dimensions
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


def parse_image_options(
    image_options: str,
    jpeg_quality: int,
) -> tuple[Callable[[Path], bool], ImageOptions]:
    path, options = (
        image_options.rsplit(":", 1) if ":" in image_options else (None, image_options)
    )
    options = options.split(",")
    size = []
    jpeg = False
    for opt in options:
        if opt.endswith("dpi"):
            size.append(DpiValue(int(opt[:-3])))
        elif opt.endswith("px"):
            size.append(PxValue(int(opt[:-2])))
        elif opt.startswith("jpeg"):
            if "@" in opt:
                jpeg_quality = int(opt.split("@")[1])
            jpeg = True
        elif opt == "":
            pass
        else:
            raise CliError(f"Invalid image option: {opt}")
    return pathspec.PathSpec.from_lines(
        "gitwildmatch", [path]
    ).match_file if path else lambda _: True, ImageOptions(size, jpeg, jpeg_quality)


def cli():
    parser = argparse.ArgumentParser(
        description="Robust arXiv LaTeX cleaner with DPI-based image rescaling."
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {metadata.version('arxivit')}",
    )
    parser.add_argument(
        "input_file",
        type=str,
        help="Path to the input LaTeX file",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        help="Path to the output. Can either be a dir or a .tar, .zip, or .tar.gz file.",
    )
    parser.add_argument(
        "--compile",
        action="store_true",
        help="Compile the processed archive and output the resulting PDF.",
    )
    parser.add_argument(
        "-i",
        "--image-options",
        type=str,
        default=[],
        action="extend",
        nargs="+",
        help="Provide options for image processing in the form '[path:]options'. path: is optional and can be in .gitignore format, options is a comma separated list of dpi, px and jpeg options. For example, --image-options 'figures/qualitative/*:300dpi,jpeg'.",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=95,
        help="Default JPEG quality (0-100) when not explicitly provided with e.g. 'jpeg@95'.",
    )

    args = parser.parse_args()

    image_options_list = [
        parse_image_options(opt, args.jpeg_quality) for opt in args.image_options
    ]

    try:
        input_file = Path(args.input_file)
        if not input_file.is_file():
            raise CliError("Input needs to be a LaTeX file (e.g., main.tex).")

        output = args.output
        if output is None:
            output = input_file.parent.resolve().with_suffix(".arxiv.tar.gz")
        output = Path(output)

        archive_format = None
        archive_base = output.with_suffix("")
        match output.suffix:
            case ".tar":
                archive_format = "tar"
            case ".zip":
                archive_format = "zip"
        if len(output.suffixes) > 1 and output.suffixes[-2:] == [".tar", ".gz"]:
            archive_format = "gztar"
            archive_base = archive_base.with_suffix("")
        if archive_format:
            with tempfile.TemporaryDirectory() as tmp_output:
                tmp_output = Path(tmp_output)
                arxivit(input_file, tmp_output, image_options_list)
                shutil.make_archive(str(archive_base), archive_format, tmp_output)
                if args.compile:
                    with console.status(Text("Compiling arXiv LaTeX")):
                        tmp_tex = tmp_output / input_file.name
                        command = [
                            "latexmk",
                            "-pdf",
                            tmp_tex,
                        ]
                        subprocess.run(
                            command,
                            cwd=tmp_tex.parent,
                            capture_output=True,
                            check=True,
                        )
                        pdf = tmp_tex.with_suffix(".pdf")
                        pdf_size = pdf.stat().st_size
                        pdf_output_dir = Path(tempfile.mkdtemp())
                        pdf = Path(shutil.copy(pdf, pdf_output_dir))
                        shutil.copy(tmp_tex.with_suffix(".log"), pdf_output_dir)
                        console.print(
                            Text("🔍 Compiled arXiv PDF saved for inspection to ")
                            + Text(str(pdf), style="bright_blue bold")
                            + Text(
                                f"  => {naturalsize(pdf_size)}",
                                style="dim",
                            )
                        )
        else:
            if args.compile:
                raise CliError("Output must be an archive when --compile is set.")
            if output.exists():
                shutil.rmtree(output)
            os.makedirs(output)
            arxivit(input_file, output, image_options_list)

        console.print(
            Text("🎉 Done! Output saved to ")
            + Text(str(output), style="bright_blue bold")
        )
    except CliError as e:
        console.log(f"Error: {e}", style="red")


if __name__ == "__main__":
    cli()
