"""
UI Validator — uses Playwright to screenshot staging and Gemini Vision
to reason about what it sees (optionally comparing against a reference
screenshot sent by the user via Telegram).
"""

import base64
import logging
import os

import vertexai
from vertexai.generative_models import GenerativeModel, Part, Image
from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)

STAGING_URL = os.environ["STAGING_URL"]
GCP_PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
GCP_REGION = os.environ.get("GOOGLE_CLOUD_REGION", "us-central1")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")


def validate_ui(
    path: str,
    prompt: str,
    reference_image_bytes: bytes | None = None,
) -> str:
    """
    1. Navigate to `path` on staging with Playwright, take a screenshot.
    2. Send screenshot (+ optional reference image) to Gemini Vision.
    3. Return Gemini's analysis as a string.
    """
    staging_screenshot = _take_screenshot(path)

    vertexai.init(project=GCP_PROJECT, location=GCP_REGION)
    model = GenerativeModel(GEMINI_MODEL)

    parts = []

    if reference_image_bytes:
        parts.append(
            Part.from_data(
                data=reference_image_bytes,
                mime_type="image/png",
            )
        )
        parts.append("This is the REFERENCE image (what it should look like).")

    parts.append(
        Part.from_data(
            data=staging_screenshot,
            mime_type="image/png",
        )
    )

    if reference_image_bytes:
        instruction = (
            f"Compare the REFERENCE image to the STAGING screenshot above. "
            f"User instruction: '{prompt}'\n\n"
            "List any visual differences, layout issues, missing elements, or bugs. "
            "Be specific — mention exact UI elements, positions, colours, or text that differ. "
            "If everything looks correct, say so clearly."
        )
    else:
        instruction = (
            f"You are reviewing a staging environment screenshot. "
            f"User instruction: '{prompt}'\n\n"
            "Describe what you see. Flag any obvious UI bugs, broken layouts, "
            "missing elements, error states, or anything that looks wrong. "
            "Be specific about what you observe."
        )

    parts.append(instruction)

    response = model.generate_content(parts)
    return response.text


def _take_screenshot(path: str) -> bytes:
    """Navigate to staging URL + path and return PNG bytes."""
    url = f"{STAGING_URL.rstrip('/')}/{path.lstrip('/')}"
    log.info(f"Taking screenshot of {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(url, wait_until="networkidle", timeout=30_000)
        screenshot = page.screenshot(full_page=False)
        browser.close()

    return screenshot
