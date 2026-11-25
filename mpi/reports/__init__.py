"""Report helpers for the live capture pipeline.

The live dashboard's CSV replay uses
``adapter.summary_to_report`` and the live xlsx export reuses
``excel_export.export_data_to_excel``.
"""

from .excel_export import export_data_to_excel

__all__ = ["export_data_to_excel"]
