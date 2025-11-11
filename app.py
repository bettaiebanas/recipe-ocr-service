import os
from io import BytesIO
from typing import List, Optional
import httpx
import pytesseract
from PIL import Image
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

app = FastAPI()
@app.get("/")
async def root():
    return {"status": "ok"}
    
INTERNAL_SECRET = os.getenv("PYTHON_RECIPE_API_SECRET", "")

@app.post("/import-recipe-from-image")
async def import_recipe_from_image(payload: ImagePayload, request: Request):
    # TEMPORAIREMENT désactivé pour faciliter les tests
    # if INTERNAL_SECRET:
    #     header_secret = request.headers.get("x-internal-secret")
    #     if header_secret != INTERNAL_SECRET:
    #         raise HTTPException(status_code=401, detail="Unauthorized")




class ImagePayload(BaseModel):
    image_url: str
    household_id: Optional[str] = None


@app.post("/import-recipe-from-image")
async def import_recipe_from_image(payload: ImagePayload, request: Request):
    # Sécurité basique entre Supabase et ce service
    if INTERNAL_SECRET:
        header_secret = request.headers.get("x-internal-secret")
        if header_secret != INTERNAL_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized")

    # 1) Télécharger l'image
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(payload.image_url)
    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Impossible de télécharger l'image")

    try:
        img = Image.open(BytesIO(resp.content))
    except Exception:
        raise HTTPException(status_code=400, detail="Image invalide")

    # 2) OCR avec Tesseract (gratuit)
    # On utilise français + anglais si dispo
    text = pytesseract.image_to_string(img, lang="fra+eng")

    if not text.strip():
        return {"ok": False, "error": "Aucun texte détecté sur l'image."}

    # 3) Parser le texte pour extraire recette + ingrédients
    recipe, ingredients = parse_recipe_from_text(text)

    if not ingredients:
        return {
            "ok": False,
            "error": "Texte détecté, mais impossible d'identifier les ingrédients.",
        }

    return {
        "ok": True,
        "recipe": recipe,
        "ingredients": ingredients,
    }


def parse_recipe_from_text(text: str):
    """
    Heuristique simple :
    - 1ère ligne non vide = nom de la recette
    - bloc après 'ingrédient' = ingrédients
    - on renvoie dans le format attendu par ton app
    """
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l]

    name = lines[0] if lines else "Recette importée"

    # Trouver la section ingrédients
    start_ing = None
    end_ing = None

    for i, line in enumerate(lines):
        low = line.lower()
        if "ingrédient" in low:
            start_ing = i + 1
            break

    if start_ing is not None:
        # Fin des ingrédients = soit mot-clé préparation, soit grosse ligne vide, soit fin
        for j in range(start_ing, len(lines)):
            low = lines[j].lower()
            if (
                "préparation" in low
                or "instruction" in low
                or "étape" in low
                or "etape" in low
            ):
                end_ing = j
                break
        if end_ing is None:
            end_ing = len(lines)
        ing_lines = lines[start_ing:end_ing]
    else:
        # fallback : on prend 5-15 lignes après le titre comme ingrédients probables
        ing_lines = lines[1:15]

    ingredients = [
        parse_ingredient_line(l) for l in ing_lines if len(l) > 2
    ]

    # Nettoyage des ingrédients vides
    ingredients = [
        ing for ing in ingredients if ing["name"]
    ]

    recipe = {
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

    return recipe, ingredients


def parse_ingredient_line(line: str):
    """
    Parsing très proche de ta fonction TS:
    '200 g de farine' -> quantity=200, unit='g', name='farine'
    """
    raw = line.strip()
    import re

    re_line = re.compile(r"^\s*(\d+(?:[.,]\d+)?)?\s*([A-Za-zÀ-ÿ\.]+)?\s*(.*)$")
    m = re_line.match(raw)

    quantity = None
    unit = None
    name = raw

    def parse_number(v: str):
        v = v.replace(",", ".")
        import re
        m = re.search(r"[\d.]+", v)
        if not m:
            return None
        try:
            return float(m.group(0))
        except ValueError:
            return None

    if m:
        if m.group(1):
            quantity = parse_number(m.group(1))
        if m.group(2):
            unit = m.group(2).lower().rstrip(".")
        if m.group(3):
            name = (
                m.group(3)
                .lstrip()
                .removeprefix("de ")
                .removeprefix("d'")
                .removeprefix("d’")
                .strip()
            )

    return {
        "raw": raw,
        "name": name,
        "quantity": quantity,
        "unit": unit,
        "category": "principal",
    }
