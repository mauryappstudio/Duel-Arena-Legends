#!/usr/bin/env python3
"""
Generador de arte de cartas para Duel Arena Legends usando la API de Leonardo.ai.

QUÉ HACE
--------
1. Lee el array `const CARDS = [...]` directamente desde index.html (así nunca
   se desincroniza de los datos reales del juego).
2. Arma un prompt por carta combinando su nombre/tipo/rareza/efecto con el
   estilo que pediste: "fantasy art, dark background, epic lighting,
   card game illustration, no text, high detail".
3. Llama a la API de Leonardo.ai (POST /generations, luego hace polling a
   GET /generations/{id}) y descarga la imagen resultante.
4. Guarda cada imagen en art/cards/<id>.jpg y va actualizando
   art/cards/manifest.json (id -> archivo, prompt usado, fecha) para poder
   cortar y retomar el proceso sin repetir cartas ya generadas.

ORDEN: legendarias primero, luego épicas (--rarities controla esto; por
defecto son las únicas dos rarezas que procesa, como pediste). Cuando quieras
seguir con raras/comunes, corré de nuevo con --rarities rare,common.

SEGURIDAD — MUY IMPORTANTE
---------------------------
Este script se corre UNA VEZ, localmente, para generar archivos de imagen.
La API Key de Leonardo.ai NUNCA debe:
  - escribirse dentro de index.html (el juego corre en el navegador del
    jugador sin backend — cualquiera vería la key con "Ver código fuente"
    y podría gastar tus créditos).
  - commitearse al repo (ni en este script, ni en un .env, ni en un log).
Pasala SIEMPRE como variable de entorno, nunca como argumento en texto plano
si podés evitarlo (los argumentos de línea de comandos a veces quedan en el
historial de la shell o en `ps`).

USO
---
    cd tools
    pip install -r requirements.txt
    export LEONARDO_API_KEY="tu-api-key-aqui"

    # 1) Primero, sin gastar créditos: ver qué modelos hay disponibles
    python3 generate_card_art.py --list-models

    # 2) Probar con 2 cartas nomás para validar el resultado visualmente
    python3 generate_card_art.py --model-id <UUID_DEL_MODELO> --limit 2

    # 3) Si el resultado te gusta, correr todas las legendarias + épicas (74 cartas)
    python3 generate_card_art.py --model-id <UUID_DEL_MODELO>

    # 4) Más adelante, seguir con el resto
    python3 generate_card_art.py --model-id <UUID_DEL_MODELO> --rarities rare,common

El script es resumible: si se corta a mitad de camino (Ctrl+C, error de red,
se acaban los créditos), simplemente corré el mismo comando de nuevo y va a
saltarse las cartas que ya tienen imagen en art/cards/manifest.json.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Falta la librería 'requests'. Corré: pip install -r requirements.txt")

API_BASE = "https://cloud.leonardo.ai/api/rest/v1"
STYLE_SUFFIX = "fantasy art, dark background, epic lighting, card game illustration, no text, high detail"
NEGATIVE_PROMPT = "text, watermark, signature, letters, words, blurry, low quality, extra limbs, deformed, cropped, frame, border, multiple panels"

RARITY_ORDER = ["legendary", "epic", "rare", "common"]

MONTYPE_FLAVOR = {
    "warrior": "armored warrior",
    "beast": "wild beast",
    "dragon": "dragon",
    "zombie": "undead creature",
    "machine": "mechanical construct",
    "rock": "stone golem",
    "aqua": "aquatic creature",
    "spellcaster": "robed spellcaster",
    "fiend": "demonic fiend",
    "winged": "winged creature",
}

HABITAT_FLAVOR = {
    "prairie": "grassy prairie",
    "forest": "dark forest",
    "mountain": "jagged mountains",
    "ocean": "deep ocean",
    "dark": "shadowy void",
    "volcano": "erupting volcano",
    "earth": "rocky earth",
    "ice": "frozen ice field",
}


def parse_cards(html_text):
    """Extrae los objetos del array `const CARDS = [ ... ];` de index.html.

    Es un parser chico y tolerante para objetos-literal de JS (no JSON real:
    claves sin comillas, strings con comillas simples). No usa un motor JS,
    solo recorre caracteres respetando comillas y llaves anidadas.
    """
    start = html_text.index("const CARDS = [")
    depth = 0
    i = html_text.index("[", start)
    body_start = i
    in_str = None
    escape = False
    j = i
    while j < len(html_text):
        c = html_text[j]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == in_str:
                in_str = None
        else:
            if c in ("'", '"'):
                in_str = c
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
        j += 1
    array_src = html_text[body_start:j]

    # Separar objetos top-level {...} dentro del array
    objs = []
    depth = 0
    in_str = None
    escape = False
    obj_start = None
    for k, c in enumerate(array_src):
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == in_str:
                in_str = None
            continue
        if c in ("'", '"'):
            in_str = c
        elif c == "{":
            if depth == 0:
                obj_start = k
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                objs.append(array_src[obj_start:k + 1])
                obj_start = None

    cards = []
    kv_re = re.compile(
        r"""(\w+)\s*:\s*(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)"|(-?\d+\.?\d*)|(true|false))"""
    )
    for obj_src in objs:
        card = {}
        for m in kv_re.finditer(obj_src):
            key = m.group(1)
            if m.group(2) is not None:
                val = m.group(2).replace("\\'", "'")
            elif m.group(3) is not None:
                val = m.group(3).replace('\\"', '"')
            elif m.group(4) is not None:
                val = float(m.group(4)) if "." in m.group(4) else int(m.group(4))
            else:
                val = m.group(5) == "true"
            card[key] = val
        if "id" in card and "name" in card:
            cards.append(card)
    return cards


def build_prompt(card):
    ctype = card.get("type", "monster")
    name = card.get("name", "")
    effect = card.get("effect", "")

    if ctype == "monster":
        flavor = MONTYPE_FLAVOR.get(card.get("monType", ""), "fantasy creature")
        habitat = HABITAT_FLAVOR.get(card.get("habitat", ""), "")
        rarity = card.get("rarity", "common")
        setting = f", {habitat} backdrop" if habitat else ""
        prompt = f"{name}, a {rarity} {flavor}{setting}, trading card game monster illustration"
    elif ctype == "spell":
        prompt = f"{name}, a magical spell effect card illustration: {effect}"
    else:  # trap
        prompt = f"{name}, an ominous hidden trap card illustration: {effect}"

    return f"{prompt}, {STYLE_SUFFIX}"


def api_headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def list_models(api_key):
    r = requests.get(f"{API_BASE}/platformModels", headers=api_headers(api_key), timeout=30)
    r.raise_for_status()
    data = r.json()
    models = data.get("custom_models") or data.get("platformModels") or data
    print(json.dumps(data, indent=2)[:4000])
    print("\n(Lista completa arriba puede estar recortada; buscá un modelo general "
          "de ilustración/fantasía y copiá su 'id' para pasarlo con --model-id)")


def generate_one(api_key, prompt, model_id, width, height, timeout_s=180):
    body = {
        "prompt": prompt,
        "negative_prompt": NEGATIVE_PROMPT,
        "modelId": model_id,
        "width": width,
        "height": height,
        "num_images": 1,
    }
    r = requests.post(f"{API_BASE}/generations", headers=api_headers(api_key), json=body, timeout=30)
    if r.status_code == 429:
        raise RuntimeError("Rate limited (429) por Leonardo.ai — bajá la velocidad o esperá.")
    r.raise_for_status()
    gen_id = r.json()["sdGenerationJob"]["generationId"]

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(4)
        rg = requests.get(f"{API_BASE}/generations/{gen_id}", headers=api_headers(api_key), timeout=30)
        rg.raise_for_status()
        gen = rg.json().get("generations_by_pk", {})
        status = gen.get("status")
        if status == "COMPLETE":
            images = gen.get("generated_images", [])
            if not images:
                raise RuntimeError(f"Generación {gen_id} completó sin imágenes.")
            return images[0]["url"]
        if status == "FAILED":
            raise RuntimeError(f"Generación {gen_id} falló del lado de Leonardo.ai.")
    raise TimeoutError(f"Generación {gen_id} no terminó en {timeout_s}s.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index-html", default="../index.html", help="Ruta a index.html")
    ap.add_argument("--out-dir", default="../art/cards", help="Carpeta de salida de imágenes")
    ap.add_argument("--api-key-env", default="LEONARDO_API_KEY", help="Variable de entorno con la API key")
    ap.add_argument("--model-id", default=None, help="UUID del modelo de Leonardo.ai a usar")
    ap.add_argument("--list-models", action="store_true", help="Solo listar modelos disponibles y salir")
    ap.add_argument("--rarities", default="legendary,epic", help="Rarezas a procesar, en orden, separadas por coma")
    ap.add_argument("--width", type=int, default=512)
    ap.add_argument("--height", type=int, default=768)
    ap.add_argument("--limit", type=int, default=None, help="Máximo de cartas a generar en esta corrida")
    ap.add_argument("--sleep", type=float, default=2.0, help="Segundos de espera entre cartas")
    ap.add_argument("--dry-run", action="store_true", help="Solo mostrar los prompts, no llamar a la API")
    args = ap.parse_args()

    api_key = os.environ.get(args.api_key_env)
    if not api_key and not args.dry_run:
        sys.exit(f"Falta la API key. Corré: export {args.api_key_env}=tu-key-aqui")

    if args.list_models:
        list_models(api_key)
        return

    html_path = Path(args.index_html)
    if not html_path.exists():
        sys.exit(f"No encontré {html_path} (usá --index-html para indicar la ruta)")
    cards = parse_cards(html_path.read_text(encoding="utf-8"))
    print(f"Cartas leídas de index.html: {len(cards)}")

    wanted_rarities = [r.strip() for r in args.rarities.split(",")]
    cards = [c for c in cards if c.get("rarity") in wanted_rarities]
    cards.sort(key=lambda c: wanted_rarities.index(c["rarity"]))
    print(f"Cartas a procesar ({','.join(wanted_rarities)}): {len(cards)}")

    if not args.dry_run and not args.model_id:
        sys.exit("Falta --model-id. Corré primero con --list-models para ver las opciones.")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    done = 0
    for card in cards:
        cid = str(card["id"])
        if cid in manifest and (out_dir / manifest[cid]["file"]).exists():
            continue
        if args.limit is not None and done >= args.limit:
            break

        prompt = build_prompt(card)
        print(f"\n[{card['rarity']:9s}] #{cid} {card['name']!r}")
        print(f"  prompt: {prompt}")

        if args.dry_run:
            continue

        try:
            url = generate_one(api_key, prompt, args.model_id, args.width, args.height)
            img_bytes = requests.get(url, timeout=60).content
            fname = f"{cid}.jpg"
            (out_dir / fname).write_bytes(img_bytes)
            manifest[cid] = {"file": fname, "name": card["name"], "prompt": prompt, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
            print(f"  ✓ guardado en {out_dir / fname}")
            done += 1
        except Exception as e:
            print(f"  ✗ ERROR: {e}")
            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

        time.sleep(args.sleep)

    print(f"\nListo. {done} carta(s) generada(s) en esta corrida.")
    print(f"Manifest: {manifest_path} ({len(manifest)} cartas en total con arte)")


if __name__ == "__main__":
    main()
