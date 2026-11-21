"""
Hello GIS - Модуль для анализа доступности парков через OSMnx.
"""

# Импортируем только модуль config
from . import config

# Остальные модули (fetch_data, visualize, network_analysis)
# вызываются напрямую через python -m, поэтому здесь их импортировать не нужно

__all__ = [
    "config",
]
