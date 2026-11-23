"""
Shared third-party imports.
Feature-specific dependencies are imported locally.
"""

# --- Geospatial ---
import logging
import os
import sys

# --- Standard library ---
from pathlib import Path

import geopandas as gpd

# --- Visualization ---
import matplotlib.pyplot as plt
import numpy as np
import osmnx as ox

# --- Data ---
import pandas as pd
import requests
import shapely.geometry as geom
from matplotlib import cm
from matplotlib.patches import Rectangle
from shapely.geometry import LineString, Point, Polygon, box
from shapely.geometry.base import BaseGeometry

__all__ = [
    "BaseGeometry",
    "LineString",
    "Path",
    "Point",
    "Polygon",
    "Rectangle",
    "box",
    "cm",
    "geom",
    "gpd",
    "logging",
    "np",
    "os",
    "ox",
    "pd",
    "plt",
    "requests",
    "sys",
]
