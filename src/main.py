"""RightMeal entry point."""

from pathlib import Path

import flet as ft
from dotenv import load_dotenv

from ui.app import main

load_dotenv()

# Serve bundled assets (recipe dish images live in src/assets/recipe_images);
# resolved relative to this module so it works in dev and packaged builds.
ASSETS_DIR = str(Path(__file__).resolve().parent / "assets")

if __name__ == "__main__":
    ft.run(main, assets_dir=ASSETS_DIR)