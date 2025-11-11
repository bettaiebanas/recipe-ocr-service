import os
from io import BytesIO
from typing import Optional

import httpx
import pytesseract
from PIL import Image
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

# ---------------------------------------------------------
# Config
# ---------------------------------------------------------

# IMPORTANT: pour les tests, on désactive le secret interne
INTERNAL_SECRET = ""  # <-- laisse vide tant que tu testes

# Langues OCR : adapte si besoin ("fra", "eng", "fra+eng", ...)
TESS_LANG = os.getenv("TESS_LANG", "fra+eng")

# ---------------------------------------------------------
# App FastAPI
# ---------------------------------------------------------

app = FastAPI(title="Recipe OCR Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_headers=["*"],
    allow_methods=["*"],
)


class ImagePayload(BaseModel):
    image_url: HttpUrl
    household_id: Optional[str] = None


# ---------------------------------------------------------
# Routes
# ---------------------------------------------------------

@app.get("/")
async def root():
    return {"status": "ok"}


@app.post("/import-recipe-from-image")
async def import_recipe_from_image(payload: ImagePayload, request: Request):
    # Secret désactivé pour les tests
    # if INTERNAL_SECRET:
    #     header_secret = request.headers.get("x-internal-secret")
    #     if header_secret != INTERNAL_SECRET:
    #         raise HTTPException(status_code=401, detail="Unauthorized")

    image_url = str(payload.image_url)

    # 1) Télécharger l'image
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(image_url)
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Erreur lors du téléchargement de l'image: {e}",
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Impossible de télécharger l'image (status {resp.status_code})",
        )

    # 2) Charger l'image
    try:
        img = Image.open(BytesIO(resp.content))
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Le fichier téléchargé n'est pas une image valide.",
        )

    # 3) OCR Tesseract
    try:
        text = pytesseract.image_to_string(img, lang=TESS_LANG)
    except pytesseract.TesseractError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Erreur OCR (Tesseract): {e}",
        )

    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "Aucun texte lisible détecté sur l'image."}

    # 4) Parsing texte -> recette + ingrédients
    recipe, ingredients = parse_recipe_from_text(text)

    if not ingredients:
        return {
            "ok": False,
            "error": "Texte détecté, mais aucun bloc d'ingrédients fiable n'a été trouvé.",
        }

    return {"ok": True, "recipe": recipe, "ingredients": ingredients}


# ---------------------------------------------------------
# Parsing
# ---------------------------------------------------------

def parse_recipe_from_text(text: str):
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    if not lines:
        return base_recipe("Recette importée"), []

    name = lines[0]
    recipe = base_recipe(name)

    start_ing = None
    end_ing = None

    for i, line in enumerate(lines):
        low = line.lower()
        if "ingrédient" in low or "ingredients" in low:
            start_ing = i + 1
            break

    if start_ing is not None:
        for j in range(start_ing, len(lines)):
            low = lines[j].lower()
            if (
                "préparation" in low
                or "preparation" in low
                or "étape" in low
                or "etape" in low
                or "instruction" in low
            ):
                end_ing = j
                break
        if end_ing is None:
            end_ing = len(lines)
        ing_lines = lines[start_ing:end_ing]
    else:
        ing_lines = lines[1:15]

    ingredients = [
        parse_ingredient_line(l) for l in ing_lines if len(l) > 2
    ]
    ingredients = [i for i in ingredients if i["name"]]

    return recipe, ingredients


def base_recipe(name: str) -> dict:
    return {
        "name": name,
        "description": None,
        "servings": None,
        "prep_time_min": None,
        "cook_time_min": None,
        "total_time_min": None,
        "difficulty": None,
        "caloric_label": None,
        "origin_code": None,
        "regime_code": None,
        "web_url": None,
        "video_url": None,
    }


def parse_ingredient_line(line: str) -> dict:
    import re

    raw = line.strip()
    if not raw:
        return {"raw": raw, "name": "", "quantity": None, "unit": None, "category": "principal"}

    pattern = re.compile(r"^\s*(\d+(?:[.,]\d+)?)?\s*([A-Za-zÀ-ÿµ%\/\-\.]+)?\s*(.*)$")
    m = pattern.match(raw)

    quantity = None
    unit = None
    name = raw

    def parse_number(v: str):
        v = v.replace(",", ".")
        m2 = re.search(r"[\d.]+", v)
        if not m2:
            return None
        try:
            return float(m2.group(0))
        except ValueError:
            return None

    if m:
        if m.group(1):
            quantity = parse_number(m.group(1))
        if m.group(2):
            unit = m.group(2).lower().rstrip(".")
        if m.group(3):
            cleaned = m.group(3).strip()
            for prefix in ("de ", "d'", "d’", "du ", "des "):
                if cleaned.lower().startswith(prefix):
                    cleaned = cleaned[len(prefix):].strip()
                    break
            name = cleaned or name

    bad_starts = (
        "préparation", "preparation", "étape", "etape",
        "pour ", "cuire", "mélanger", "melanger",
    )
    if name and name.lower().startswith(bad_starts):
        name = ""

    return {
        "raw": raw,
        "name": name,
        "quantity": quantity,
        "unit": unit,
        "category": "principal",
    }
