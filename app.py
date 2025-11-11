import os
import base64
from io import BytesIO
from typing import Optional

import httpx
import pytesseract
from PIL import Image
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------
# Config
# ---------------------------------------------------------

# Optionnel : si tu mets PYTHON_RECIPE_API_SECRET dans Render,
# la fonction exigera le header x-internal-secret avec la même valeur.
INTERNAL_SECRET = os.getenv("PYTHON_RECIPE_API_SECRET", "").strip()

@app.post("/import-recipe-from-image")
async def import_recipe_from_image(payload: ImagePayload, request: Request):
    # Sécurité optionnelle : n'active que si la variable est NON vide
    if INTERNAL_SECRET:
        header_secret = request.headers.get("x-internal-secret")
        if header_secret != INTERNAL_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized")


# Langues OCR utilisées par Tesseract
TESS_LANG = os.getenv("TESS_LANG", "fra+eng")

# ---------------------------------------------------------
# App
# ---------------------------------------------------------

app = FastAPI(title="Recipe OCR Service", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_headers=["*"],
    allow_methods=["*"],
)


class ImagePayload(BaseModel):
    # On accepte maintenant soit une URL http(s), soit une data URL base64
    image_url: str
    household_id: Optional[str] = None


# ---------------------------------------------------------
# Routes
# ---------------------------------------------------------

@app.get("/")
async def root():
    return {"status": "ok"}


@app.post("/import-recipe-from-image")
async def import_recipe_from_image(payload: ImagePayload, request: Request):
    # Sécurité optionnelle
    if INTERNAL_SECRET:
        header_secret = request.headers.get("x-internal-secret")
        if header_secret != INTERNAL_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized")

    image_source = payload.image_url.strip()

    # 1) Récupérer l'image (data URL ou URL distante)
    img = await load_image(image_source)

    # 2) OCR Tesseract
    try:
        text = pytesseract.image_to_string(img, lang=TESS_LANG)
    except pytesseract.TesseractError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Erreur OCR (Tesseract): {e}",
        )

    text = (text or "").strip()
    if not text:
        return {
            "ok": False,
            "error": "Aucun texte lisible détecté sur l'image.",
        }

    # 3) Parsing texte -> recette + ingrédients
    recipe, ingredients = parse_recipe_from_text(text)

    if not ingredients:
        return {
            "ok": False,
            "error": "Texte détecté, mais aucun bloc d'ingrédients fiable n'a été trouvé.",
        }

    return {
        "ok": True,
        "recipe": recipe,
        "ingredients": ingredients,
    }


# ---------------------------------------------------------
# Chargement image
# ---------------------------------------------------------

async def load_image(source: str) -> Image.Image:
    """
    - Si source commence par 'data:image', on décode le base64.
    - Sinon, si c'est http/https, on télécharge.
    - Sinon, erreur.
    """
    if source.startswith("data:image"):
        # data URL: data:image/png;base64,xxxxx
        try:
            header, b64 = source.split(",", 1)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Data URL invalide.",
            )
        try:
            binary = base64.b64decode(b64)
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="Impossible de décoder l'image en base64.",
            )
        try:
            img = Image.open(BytesIO(binary))
            img.load()
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="Data URL ne contient pas une image valide.",
            )
        return img

    # URL classique
    if source.startswith("http://") or source.startswith("https://"):
        try:
            async with httpx.AsyncClient(
                timeout=20.0, follow_redirects=True
            ) as client:
                resp = await client.get(source)
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Erreur lors du téléchargement de l'image: {e}",
            )

        if resp.status_code != 200:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Impossible de télécharger l'image "
                    f"(status {resp.status_code})"
                ),
            )

        try:
            img = Image.open(BytesIO(resp.content))
            img.load()
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="Le fichier téléchargé n'est pas une image valide.",
            )

        return img

    # Format non supporté
    raise HTTPException(
        status_code=400,
        detail="Le champ 'image_url' doit être une URL http(s) ou une data URL base64.",
    )


# ---------------------------------------------------------
# Parsing recette / ingrédients
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

    ingredients = [parse_ingredient_line(l) for l in ing_lines if len(l) > 2]
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
        return {
            "raw": raw,
            "name": "",
            "quantity": None,
            "unit": None,
            "category": "principal",
        }

    pattern = re.compile(
        r"^\s*(\d+(?:[.,]\d+)?)?\s*([A-Za-zÀ-ÿµ%/\-\.]+)?\s*(.*)$"
    )
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
        "préparation",
        "preparation",
        "étape",
        "etape",
        "pour ",
        "cuire",
        "mélanger",
        "melanger",
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
