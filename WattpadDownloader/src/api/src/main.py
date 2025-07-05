"""WattpadDownloader API Server."""

import asyncio
from enum import Enum
from io import BytesIO
import zipfile
from pathlib import Path
from typing import Optional
from zipfile import ZipFile
import zipfile

from aiohttp import ClientResponseError
from bs4 import BeautifulSoup
from eliot import start_action
from PIL import Image, ImageDraw, ImageFont
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from create_book import (
    EPUBGenerator,
    PDFGenerator,
    StoryNotFoundError,
    WattpadError,
    fetch_cookies,
    fetch_image,
    fetch_story,
    fetch_story_content_zip,
    fetch_story_from_partId,
    logger,
    slugify,
)
from create_book.parser import clean_tree, fetch_tree_images
from create_book.epub_to_pdf_converter import convert_epub_to_pdf

app = FastAPI()
BUILD_PATH = Path(__file__).parent.parent.parent / "frontend" / "build"


class RequestCancelledMiddleware:
    # Thanks https://github.com/fastapi/fastapi/discussions/11360#discussion-6427734
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Let's make a shared queue for the request messages
        queue = asyncio.Queue()

        async def message_poller(sentinel, handler_task):
            nonlocal queue
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    handler_task.cancel()
                    return sentinel  # Break the loop

                # Puts the message in the queue
                await queue.put(message)

        sentinel = object()
        handler_task = asyncio.create_task(self.app(scope, queue.get, send))
        asyncio.create_task(message_poller(sentinel, handler_task))

        try:
            return await handler_task
        except asyncio.CancelledError:
            logger.info("Cancelling task as connection closed")


app.add_middleware(RequestCancelledMiddleware)


class DownloadFormat(Enum):
    pdf = "pdf"
    epub = "epub"
    epub_and_pdf = "epub_and_pdf"  # Generate EPUB first, then convert to PDF, return both
    both = "both"


class DownloadMode(Enum):
    story = "story"
    part = "part"


@app.get("/")
def home():
    return FileResponse(BUILD_PATH / "index.html")


@app.exception_handler(ClientResponseError)
def download_error_handler(request: Request, exception: ClientResponseError):
    match exception.status:
        case 400 | 404:
            return HTMLResponse(
                status_code=404,
                content='This story does not exist, or has been deleted. Support is available on the <a href="https://discord.gg/P9RHC4KCwd" target="_blank">Discord</a>',
            )
        case 429:
            # Rate-limit by Wattpad
            return HTMLResponse(
                status_code=429,
                content='The website is overloaded. Please try again in a few minutes. Support is available on the <a href="https://discord.gg/P9RHC4KCwd" target="_blank">Discord</a>',
            )
        case _:
            # Unhandled error
            return HTMLResponse(
                status_code=500,
                content='Something went wrong. Yell at me on the <a href="https://discord.gg/P9RHC4KCwd" target="_blank">Discord</a>',
            )


@app.exception_handler(WattpadError)
def wattpad_error_handler(request: Request, exception: WattpadError):
    if isinstance(exception, StoryNotFoundError):
        return HTMLResponse(
            status_code=404,
            content='This story does not exist, or has been deleted. Support is available on the <a href="https://discord.gg/P9RHC4KCwd" target="_blank">Discord</a>',
        )
    return HTMLResponse(
        status_code=400,
        content=f'Error: {str(exception)}. Support is available on the <a href="https://discord.gg/P9RHC4KCwd" target="_blank">Discord</a>',
    )


@app.get("/download/{download_id}")
async def handle_download(
    download_id: int,
    download_images: bool = False,
    mode: DownloadMode = DownloadMode.story,
    format: DownloadFormat = DownloadFormat.epub,
    username: Optional[str] = None,
    password: Optional[str] = None,
):
    with start_action(
        action_type="download",
        download_id=download_id,
        download_images=download_images,
        format=format,
        mode=mode,
    ):
        if username and not password or password and not username:
            logger.error(
                "Username with no Password or Password with no Username provided."
            )
            return HTMLResponse(
                status_code=422,
                content='Include both the username <u>and</u> password, or neither. Support is available on the <a href="https://discord.gg/P9RHC4KCwd" target="_blank">Discord</a>',
            )

        if username and password:
            # username and password are URL-Encoded by the frontend. FastAPI automatically decodes them.
            try:
                cookies = await fetch_cookies(username=username, password=password)
            except ValueError:
                logger.error("Invalid username or password.")
                return HTMLResponse(
                    status_code=403,
                    content='Incorrect Username and/or Password. Support is available on the <a href="https://discord.gg/P9RHC4KCwd" target="_blank">Discord</a>',
                )
        else:
            cookies = None

        match mode:
            case DownloadMode.story:
                story_id = download_id
                metadata = await fetch_story(story_id, cookies)
            case DownloadMode.part:
                story_id, metadata = await fetch_story_from_partId(download_id, cookies)

        cover_data = await fetch_image(
            metadata["cover"].replace("-256-", "-512-")
        )  # Increase resolution
        if not cover_data:
            raise HTTPException(status_code=422)

        story_zip = await fetch_story_content_zip(story_id, cookies)
        archive = ZipFile(story_zip, "r")

        part_trees: list[BeautifulSoup] = [
            clean_tree(
                part["title"], part["id"], archive.read(str(part["id"])).decode("utf-8")
            )
            for part in metadata["parts"]
        ]

        images = (
            [await fetch_tree_images(tree) for tree in part_trees]
            if download_images
            else []
        )

        match format:
            case DownloadFormat.epub:
                book = EPUBGenerator(metadata, part_trees, cover_data, images)
                media_type = "application/epub+zip"
                
                logger.info(f"Retrieved story metadata and cover ({story_id=})")
                book.compile()
                book_buffer = book.dump()
                
                async def iterfile():
                    while chunk := book_buffer.read(512 * 4):  # 4 kb/s
                        await asyncio.sleep(0.1)  # throttle download speed
                        yield chunk

                return StreamingResponse(
                    iterfile(),
                    media_type=media_type,
                    headers={
                        "Content-Disposition": f'attachment; filename="{slugify(metadata["title"])}_{story_id}{"_images" if download_images else ""}.epub"'
                    },
                )
            
            case DownloadFormat.epub_and_pdf:
                # Generate EPUB first
                epub_book = EPUBGenerator(metadata, part_trees, cover_data, images)
                logger.info(f"Retrieved story metadata and cover ({story_id=})")
                epub_book.compile()
                epub_buffer = epub_book.dump()
                
                # Reset buffer position for PDF conversion
                epub_buffer.seek(0)
                
                # Convert EPUB to PDF
                try:
                    pdf_buffer = convert_epub_to_pdf(epub_buffer)
                    logger.info(f"Successfully converted EPUB to PDF ({story_id=})")
                except Exception as e:
                    logger.error(f"Failed to convert EPUB to PDF: {e}")
                    raise HTTPException(status_code=500, detail="Failed to convert EPUB to PDF")
                
                # Create a ZIP file containing both formats
                zip_buffer = BytesIO()
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                    # Add EPUB file
                    epub_filename = f"{slugify(metadata['title'])}_{story_id}{'_images' if download_images else ''}.epub"
                    zip_file.writestr(epub_filename, epub_buffer.getvalue())
                    
                    # Add PDF file (converted from EPUB)
                    pdf_filename = f"{slugify(metadata['title'])}_{story_id}{'_images' if download_images else ''}.pdf"
                    zip_file.writestr(pdf_filename, pdf_buffer.getvalue())
                
                zip_buffer.seek(0)
                
                async def iterfile():
                    while chunk := zip_buffer.read(512 * 4):  # 4 kb/s
                        await asyncio.sleep(0.1)  # throttle download speed
                        yield chunk

                return StreamingResponse(
                    iterfile(),
                    media_type="application/zip",
                    headers={
                        "Content-Disposition": f'attachment; filename="{slugify(metadata["title"])}_{story_id}{"_images" if download_images else ""}_epub_and_pdf.zip"'
                    },
                )
            
            case DownloadFormat.pdf:
                author_image = await fetch_image(
                    metadata["user"]["avatar"].replace("-256-", "-512-")
                )
                if not author_image:
                    raise HTTPException(status_code=422)

                book = PDFGenerator(
                    metadata, part_trees, cover_data, images, author_image
                )
                media_type = "application/pdf"
                
                logger.info(f"Retrieved story metadata and cover ({story_id=})")
                book.compile()
                book_buffer = book.dump()
                
                async def iterfile():
                    while chunk := book_buffer.read(512 * 4):  # 4 kb/s
                        await asyncio.sleep(0.1)  # throttle download speed
                        yield chunk

                return StreamingResponse(
                    iterfile(),
                    media_type=media_type,
                    headers={
                        "Content-Disposition": f'attachment; filename="{slugify(metadata["title"])}_{story_id}{"_images" if download_images else ""}.pdf"'
                    },
                )
            
            case DownloadFormat.both:
                # Generate EPUB first
                epub_book = EPUBGenerator(metadata, part_trees, cover_data, images)
                epub_book.compile()
                epub_buffer = epub_book.dump()
                
                # Convert EPUB to PDF using calibre
                try:
                    from .create_book.epub_to_pdf_converter import convert_epub_to_pdf
                    pdf_buffer = convert_epub_to_pdf(epub_buffer)
                except Exception as e:
                    logger.error(f"Failed to convert EPUB to PDF: {e}")
                    raise HTTPException(status_code=500, detail="PDF conversion failed")
                
                logger.info(f"Generated EPUB and converted to PDF ({story_id=})")
                
                # Create a ZIP file containing both formats
                zip_buffer = BytesIO()
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                    # Add EPUB file
                    epub_filename = f"{slugify(metadata['title'])}_{story_id}{'_images' if download_images else ''}.epub"
                    zip_file.writestr(epub_filename, epub_buffer.getvalue())
                    
                    # Add PDF file
                    pdf_filename = f"{slugify(metadata['title'])}_{story_id}{'_images' if download_images else ''}.pdf"
                    zip_file.writestr(pdf_filename, pdf_buffer.getvalue())
                
                zip_buffer.seek(0)
                
                async def iterfile():
                    while chunk := zip_buffer.read(512 * 4):  # 4 kb/s
                        await asyncio.sleep(0.1)  # throttle download speed
                        yield chunk

                return StreamingResponse(
                    iterfile(),
                    media_type="application/zip",
                    headers={
                        "Content-Disposition": f'attachment; filename="{slugify(metadata["title"])}_{story_id}{"_images" if download_images else ""}_both_formats.zip"'
                    },
                )





@app.get("/donate")
def donate():
    """Redirect to donation URL."""
    return RedirectResponse("https://buymeacoffee.com/theonlywayup")


# Mount static files from frontend build AFTER all API routes
if BUILD_PATH.exists():
    app.mount("/static", StaticFiles(directory=BUILD_PATH / "_app"), name="static")
    app.mount("/", StaticFiles(directory=BUILD_PATH, html=True), name="spa")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5000)
