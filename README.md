# arxivit 🧹

[![image](https://img.shields.io/pypi/v/arxivit.svg)](https://pypi.python.org/pypi/arxivit)

Robust arXiv LaTeX cleaner with DPI-based image rescaling.

## Installation

```bash
pip install arxivit
```

## Usage  

```bash
# Create an uploadable archive: /path/to/paper.arxiv.tar.gz
arxivit /path/to/paper/main.tex

# Convert images to JPEG at max 300 DPI in the final PDF and save to dir /path/to/output
arxivit /path/to/paper/main.tex --image-options 'jpeg,300dpi' --output /path/to/output

# Convert images to max 300 DPI in the final PDF, images in figures/pca/ to max 1000px
arxivit /path/to/paper/main.tex --image-options '300dpi' --image-options 'figures/pca/*:1000px'
```

For more options, run:  

```bash
arxivit --help
```

## Q&A

### Why Not `arxiv-latex-cleaner`?  

`arxiv-latex-cleaner` statically analyzes LaTeX source code, which has some limitations:  

1. It does not reliably track dependencies in all scenarios—e.g., when images are included via complex macros that obscure their filenames in the source code.  
2. It only supports fixed-size image rescaling, without considering how large an image appears in the final compiled PDF.  

### How Does `arxivit` Work?  

`arxivit` takes a different approach:  

- It uses `latexmk` to compile the LaTeX source code.  
- It analyzes the compilation log to determine which `.tex` files and images are included, as well as their sizes in the final PDF.  
- It rescales images to the desired DPI based on their actual dimensions in the final document, ensuring uniform sharpness while minimizing file size.  
- It uses `latexpand` to strip comments from `.tex` files.  

## Related Projects
- https://github.com/google-research/arxiv-latex-cleaner
- https://github.com/djsutherland/arxiv-collector
