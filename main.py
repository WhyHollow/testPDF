import asyncio
import base64
import os
import re
import tempfile

import time
import subprocess
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Literal, Optional
from uuid import uuid4
from tempfile import NamedTemporaryFile
from typing import List
import aiofiles
import aiohttp
import httpx

import jwt
import json
import stripe
from cryptography.hazmat.primitives import serialization
from cryptography.x509 import load_pem_x509_certificate
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from pydub import AudioSegment

import platogram as plato

SCOPES = [
    "https://mail.google.com/",
]



app = FastAPI()



app.add_middleware(CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],)

AUTH0_DOMAIN = "dev-w0dm4z23pib7oeui.us.auth0.com"
API_AUDIENCE = "https://platogram.vercel.app/"
ALGORITHMS = ["RS256"]
JWKS_URL = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"


tasks = {}
processes = {}
Language = Literal["en", "es"]


class ConversionRequest(BaseModel):
    payload: str
    lang: Language = "en"
    price: Optional[float] = None
    token: Optional[str] = None


class Task(BaseModel):
    start_time: datetime
    request: ConversionRequest
    status: Literal["running", "done", "failed"] = "running"
    error: Optional[str] = None


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


# Cache for the Auth0 public key
auth0_public_key_cache = {
    "key": None,
    "last_updated": 0,
    "expires_in": 3600,  # Cache expiration time in seconds (1 hour)
}


@app.get("/")
async def index():
    return RedirectResponse(url="https://platogram.vercel.app")

async def get_auth0_public_key():
    current_time = time.time()

    # Check if the cached key is still valid
    if (
        auth0_public_key_cache["key"]
        and current_time - auth0_public_key_cache["last_updated"]
        < auth0_public_key_cache["expires_in"]
    ):
        return auth0_public_key_cache["key"]

    # If not, fetch the JWKS from Auth0
    async with httpx.AsyncClient() as client:
        response = await client.get(JWKS_URL)
        response.raise_for_status()
        jwks = response.json()

    x5c = jwks["keys"][0]["x5c"][0]

    # Convert the X.509 certificate to a public key
    cert = load_pem_x509_certificate(
        f"-----BEGIN CERTIFICATE-----\n{x5c}\n-----END CERTIFICATE-----".encode()
    )
    public_key = cert.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    # Update the cache
    auth0_public_key_cache["key"] = public_key
    auth0_public_key_cache["last_updated"] = current_time

    return public_key

async def verify_token_and_get_user_id(token: str = Depends(oauth2_scheme)):
    try:
        public_key = await get_auth0_public_key()
        payload = jwt.decode(
            token,
            key=public_key,
            algorithms=ALGORITHMS,
            audience=API_AUDIENCE,
            issuer=f"https://{AUTH0_DOMAIN}/",
        )
        email = payload.get("platogram:user_email") or payload.get("email")
        if not email:
            raise HTTPException(status_code=401, detail="Email not found in token")
        return email
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidAudienceError:
        raise HTTPException(status_code=401, detail="Invalid audience")
    except jwt.InvalidIssuerError:
        raise HTTPException(status_code=401, detail="Invalid issuer")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except Exception as e:
        raise HTTPException(status_code=401, detail="Couldn't verify token")

@app.post("/convert")
async def convert(
    background_tasks: BackgroundTasks,
    user_id: str = Depends(verify_token_and_get_user_id),
    file: Optional[UploadFile] = File(None),
    payload: Optional[str] = Form(None),
    lang: Optional[str] = Form(None),
    price: Optional[float] = Form(None),
    token: Optional[str] = Form(None),
):
    if lang is None:
        lang = "en"

    if user_id in tasks and tasks[user_id].status == "running":
        raise HTTPException(status_code=400, detail="Conversion already in progress")

    if payload is None and file is None:
        raise HTTPException(status_code=400, detail="Either payload or file must be provided")

    if payload is not None:
        if is_youtube_url(payload):
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://mango.sievedata.com/v2/push",
                    headers={
                        "Content-Type": "application/json",
                        "X-API-Key": "B6s3PV-pbYz52uK9s-0dIC9LfMU09RoCwRokiGjjPq4",
                    },
                    json={
                        "function": "sieve/youtube_to_mp4",
                        "inputs": {
                            "url": payload,
                            "resolution": "lowest-available",
                            "include_audio": True
                        }
                    }
                )

                if response.status_code != 200:
                    raise HTTPException(status_code=response.status_code, detail="Failed to download video from YouTube")

                job_id = response.json().get('id')
                video_url = await wait_for_job_completion(client, job_id)

            if not video_url:
                raise HTTPException(status_code=500, detail="Failed to retrieve the video URL from Sieve")
            temp_file_path = await youtube_download_and_save_file(video_url)

            request = ConversionRequest(payload=f"file://{temp_file_path}", lang=lang, price=price, token=token)
        else:
            request = ConversionRequest(payload=payload, lang=lang, price=price, token=token)
    else:
        tmpdir = Path(tempfile.gettempdir()) / "platogram_uploads"
        tmpdir.mkdir(parents=True, exist_ok=True)
        file_ext = file.filename.split(".")[-1]
        temp_file = Path(tmpdir) / f"{uuid4()}.{file_ext}"
        file_content = await file.read()
        with open(temp_file, "wb") as fd:
            fd.write(file_content)

        request = ConversionRequest(payload=f"file://{temp_file}", lang=lang)

    tasks[user_id] = Task(start_time=datetime.now(), request=request, price=price, token=token)
    background_tasks.add_task(convert_and_send_with_error_handling, request, user_id)

    return {"message": "Conversion started"}


@app.get("/status")
async def status(user_id: str = Depends(verify_token_and_get_user_id)) -> dict:
    if user_id not in tasks:
        return {"status": "idle"}
    if tasks[user_id].status == "running":
        return {"status": "running"}
    if tasks[user_id].status == "failed":
        return {"status": "failed", "error": tasks[user_id].error}
    if tasks[user_id].status == "done":
        return {"status": "done"}
    return {"status": "idle"}

@app.get("/reset")
async def reset(user_id: str = Depends(verify_token_and_get_user_id)):
    if user_id in processes:
        processes[user_id].terminate()
        del processes[user_id]

    if user_id in tasks:
        del tasks[user_id]

    return {"message": "Session reset"}

async def wait_for_job_completion(client, job_id):
    for _ in range(60):
        job_status_response = await client.get(
            f"https://mango.sievedata.com/v2/jobs/{job_id}",
            headers={
                "X-API-Key": "B6s3PV-pbYz52uK9s-0dIC9LfMU09RoCwRokiGjjPq4",
            }
        )
        if job_status_response.status_code != 200:
            raise HTTPException(status_code=job_status_response.status_code, detail="Failed to fetch sieve job status")
        job_data = job_status_response.json()

        if job_data.get('status') == 'finished':
            outputs = job_data.get('outputs', [])
            if outputs:
                # Get the URL from the first output item
                file_output = outputs[0].get('data', {})
                url = file_output.get('url')

                if url:
                    return url
        await asyncio.sleep(5)

    raise HTTPException(status_code=500, detail="Job did not complete in time")

async def youtube_download_and_save_file(file_url: str) -> Path:
    tmpdir = Path(tempfile.gettempdir()) / "platogram_uploads"
    tmpdir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient() as client:
        response = await client.get(file_url)
        response.raise_for_status()

        content_type = response.headers.get('Content-Type', '')
        original_file_ext = '.dat'

        if 'audio' in content_type:
            original_file_ext = '.mp3'
        elif 'video' in content_type:
            original_file_ext = '.mp4'



        original_file_name = f"{uuid4().hex[:8]}{original_file_ext}"
        original_file_path = tmpdir / original_file_name


        with open(original_file_path, "wb") as fd:
            fd.write(response.content)


        if original_file_ext != '.mp3':
            audio = AudioSegment.from_file(original_file_path)
            mp3_file_name = f"{uuid4().hex[:8]}.mp3"
            mp3_file_path = tmpdir / mp3_file_name

            audio.export(mp3_file_path, format="mp3")

            original_file_path.unlink()

            return mp3_file_path

    return original_file_path

def is_youtube_url(url: str) -> bool:
    youtube_regex = (
        r'(https?://)?(www\.)?'
        '(youtube\.com/watch\?v=|youtu\.be/)'
        '[\w-]{11}'
    )
    return re.match(youtube_regex, url) is not None


async def audio_to_paper(
    url: str, lang: Language, output_dir: Path, user_id: str
) -> tuple[str, str]:
    # Get absolute path of current working directory
    script_path = Path().resolve() / "audio_to_paper.sh"
    command = f'cd {output_dir} && {script_path} "{url}" --lang {lang} --verbose'

    if user_id in processes:
        raise RuntimeError("Conversion already in progress.")

    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        shell=True,
    )
    processes[user_id] = process

    try:
        stdout, stderr = await process.communicate()
    finally:
        if user_id in processes:
            del processes[user_id]

    if process.returncode != 0:
        raise RuntimeError(f"""Failed to execute {command} with return code {process.returncode}.

stdout:
{stdout.decode()}

stderr:
{stderr.decode()}""")
    return stdout.decode(), stderr.decode()

async def send_email(user_id: str, subj: str, body: str, files: List[Path]):
    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {os.getenv('RESEND_API_KEY')}",
        "Content-Type": "application/json"
    }

    payload = {
        "from": "Ivan Cherepukhin <ivan@shrinked.ai>",
        "to": user_id,
        "subject": subj,
        "text": body,
        "attachments": []
    }


    for attachment in files:
        async with aiofiles.open(attachment, "rb") as file:
            content = await file.read()
            encoded_content = base64.b64encode(content).decode('utf-8')
            payload["attachments"].append({
                "filename": attachment.name,
                "content": encoded_content
            })
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                raise Exception(f"Failed to send email. Status: {response.status}, Response: {await response.text()}")
            else:
                await check_and_add_user(user_id)
            return await response.text()

async def convert_and_send_with_error_handling(
    request: ConversionRequest, user_id: str
):
    try:
        await convert_and_send(request, user_id)
        tasks[user_id].status = "done"
        print(f"No charge for user {user_id}")
    except Exception as e:
        print(f"Error in background task for user {user_id}: {str(e)}")
        error = str(e)
        model = plato.llm.get_model("anthropic/claude-3-5-sonnet", key=os.getenv("ANTHROPIC_API_KEY"))
        error = model.prompt_model(messages=[
            plato.types.User(
                content=f"""
                Given the following error message, provide a concise, user-friendly explanation
                that focuses on the key issue and any actionable steps. Avoid technical jargon
                and keep the message under 256 characters:

                Error: {error}
                """
            )
        ])
        error = error.strip()
        tasks[user_id].error = error
        tasks[user_id].status = "failed"

async def convert_and_send(request: ConversionRequest, user_id: str):
    with tempfile.TemporaryDirectory() as tmpdir:

        if not (
            request.payload.startswith("http")
            or request.payload.startswith("file:///tmp/platogram_uploads")
        ):
            raise HTTPException(status_code=400, detail="Please provide a valid URL.")
        else:
            url = request.payload

        try:

           stdout, stderr = await audio_to_paper(url, request.lang, Path(tmpdir), user_id)

        finally:
            if request.payload.startswith("file:///tmp/platogram_uploads"):
                try:
                    os.remove(
                        request.payload.replace(
                            "file:///tmp/platogram_uploads", "/tmp/platogram_uploads"
                        )
                    )
                except OSError as e:
                    print(
                        f"Failed to delete temporary file {request.payload}: {e}"
                    )

        title_match = re.search(r"<title>(.*?)</title>", stdout, re.DOTALL)
        if title_match:
            title = title_match.group(1).strip()
        else:
            title = "👋"


        abstract_match = re.search(r"<abstract>(.*?)</abstract>", stdout, re.DOTALL)
        if abstract_match:
            abstract = abstract_match.group(1).strip()
        else:
            abstract = ""


        files = [f for f in Path(tmpdir).glob("*") if f.is_file()]

        subject = f"Your Content, Shrunk and Structured: {title}"
        body = f"""Greetings!

Your audio content has been transformed into actionable intelligence.
Attached you'll find two PDF documents:

1. A comprehensive version, including the original transcript and detailed references.
2. A streamlined version, focusing on key insights without transcript and references.

These documents have been shrunk and structured for both human comprehension and AI integration with platforms like ChatGPT or Claude, enabling deeper, more contextual prompts.

In a nutshell: {abstract}

We welcome your feedback, suggestions, or questions. Please reply to this email to help us enhance your experience with our service.

---
Cya,
Artyom & Ivan"""

        # Отправка email
        await send_email(user_id, subject, body, files)


async def _send_email_sync(user_id: str, subj: str, body: str, files: list[Path]):
    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer { os.getenv('RESEND_API_KEY')}",
        "Content-Type": "application/json"
    }
    payload = {
        "from": "Platogram <onboarding@resend.dev>",
        "to": user_id,
        "subject": subj,
        "text": body,
        "attachments": []
    }

    # Attach files if provided
    for attachment in files:
        async with aiofiles.open(attachment, "rb") as file:
            content = await file.read()
            encoded_content = base64.b64encode(content).decode('utf-8')
            payload["attachments"].append({
                "filename": attachment.name,
                "content": encoded_content
            })

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            return response

async def check_and_add_user(user_id: str):
    api_key = os.getenv('RESEND_API_KEY')
    if not api_key:
        raise HTTPException(status_code=500, detail="API key is not set in environment variables.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    base_url = 'https://api.resend.com/audiences/e8c5a23e-ff4a-4b07-917c-9f9cd4325c4f/contacts'

    async with aiohttp.ClientSession() as session:
        # Step 1: Retrieve ALL contacts
        async with session.get(base_url, headers=headers) as response:
            if response.status != 200:
                print(f"Failed to get contacts. Status: {response.status}, Response: {await response.text()}")
                raise HTTPException(status_code=response.status, detail="Failed to retrieve contacts")

            data = await response.json()
            contacts = data.get("data", [])

        # Step 2: Check if user's email is in the list
        user_exists = any(contact['email'] == user_id for contact in contacts)

        # Step 3 & 4: If user doesn't exist, try to add once
        if not user_exists:
            payload = {"email": user_id}

            async with session.post(base_url, headers=headers, json=payload) as response:
                if response.status == 201:
                    print(f"User {user_id} has been added to the contact list.")
                    return {"status": "added", "message": f"User {user_id} has been added to the contact list."}
                else:
                    print(f"Failed to add user. Status: {response.status}, Response: {await response.text()}")
                    raise HTTPException(status_code=response.status, detail="Failed to add user")
        else:
            print(f"User {user_id} already exists in the contact list.")
            return {"status": "exists", "message": f"User {user_id} already exists in the contact list."}

def send_with_retry(service, message_body, max_retries=5, initial_delay=1):
    for attempt in range(max_retries):
        try:
            return service.users().messages().send(userId="me", body=message_body).execute()
        except HttpError as error:
            if error.resp.status in [500, 503]:  # Internal Server Error or Service Unavailable
                delay = initial_delay * (2 ** attempt)
                time.sleep(delay)
            else:
                raise error

def delete_with_retry(service, message_id, max_retries=5, initial_delay=1):
    for attempt in range(max_retries):
        try:
            service.users().messages().delete(userId="me", id=message_id).execute()
            return
        except HttpError as error:
            if error.resp.status in [500, 503]:  # Internal Server Error or Service Unavailable
                delay = initial_delay * (2 ** attempt)
                time.sleep(delay)
            else:
                raise error

def get_gmail_service():
    credentials = service_account.Credentials.from_service_account_file(
        ".id/google-service-account.json", scopes=SCOPES)
    user_email = os.getenv("PLATOGRAM_USER_EMAIL")
    delegated_credentials = credentials.with_subject(user_email)

    return delegated_credentials

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
