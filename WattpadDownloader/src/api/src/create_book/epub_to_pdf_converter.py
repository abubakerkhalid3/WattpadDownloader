
"""EPUB to PDF Converter using calibre's ebook-convert."""

import subprocess
import tempfile
from io import BytesIO
from pathlib import Path


def convert_epub_to_pdf(epub_file: BytesIO) -> BytesIO:
    """
    Convert EPUB file to PDF using calibre's ebook-convert.
    
    Args:
        epub_file: BytesIO object containing the EPUB file data
        
    Returns:
        BytesIO object containing the PDF file data
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        
        # Write EPUB to temporary file
        epub_path = temp_path / "input.epub"
        pdf_path = temp_path / "output.pdf"
        
        with open(epub_path, "wb") as f:
            f.write(epub_file.getvalue())
        
        # Convert EPUB to PDF using calibre
        try:
            subprocess.run([
                "ebook-convert", 
                str(epub_path), 
                str(pdf_path),
                "--pdf-engine", "weasyprint",
                "--margin-top", "50",
                "--margin-bottom", "50", 
                "--margin-left", "50",
                "--margin-right", "50"
            ], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to convert EPUB to PDF: {e.stderr.decode()}")
        except FileNotFoundError:
            raise RuntimeError("calibre's ebook-convert not found. Please install calibre.")
        
        # Read the generated PDF
        with open(pdf_path, "rb") as f:
            pdf_data = f.read()
        
        return BytesIO(pdf_data)
