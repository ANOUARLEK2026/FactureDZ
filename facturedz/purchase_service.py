# -*- coding: utf-8 -*-
"""Purchase invoices: validation, issuing (PDF + Excel through the SAME export code as sale invoices), editing,
deleting and re-exporting. No GUI code."""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .amounts import MAX_AMOUNT
from .config import DESCRIPTION_SOFT_LIMIT, MAX_ITEMS, PURCHASE_FOLDER
from .numbering import NumberingError
from .purchasing import to_export_data
from .service import (InvoiceService, IssueResult, Progress, ValidationError, compute_items, compute_totals,
                      date_to_date, normalize_date, normalize_payment, parse_rate, unique_path)
from .textnorm import safe_filename

log = logging.getLogger("facturedz.purchases")


@dataclass
class PurchaseResult(IssueResult):
    created_products: List[str] = field(default_factory=list)


def build_purchase(form: Dict[str, Any], buyer: Dict[str, str]) -> Dict[str, Any]:
    """Validate a purchase form and return the canonical purchase dict (supplier, buyer = our business, items...)."""
    supplier = {k: str(v or "").strip() for k, v in form.get("supplier", {}).items()}
    if not supplier.get("name"):
        raise ValidationError("أدخل اسم المورد أو اختر ملفًا محفوظًا.", "supplier.name")
    items = compute_items(form.get("items", []), strict=True)
    if not items:
        raise ValidationError("أضف صنفًا واحدًا على الأقل مع الكمية وسعر الشراء.", "items.1.description")
    if len(items) > MAX_ITEMS:
        raise ValidationError(f"الحد الأقصى {MAX_ITEMS} صنفًا في فاتورة الشراء الواحدة.", "items")
    for item in items:                        # a purchased line becomes a stock product: its name must fit
        if len(item["description"]) > DESCRIPTION_SOFT_LIMIT:
            raise ValidationError(f"اسم الصنف في السطر {item['row']} أطول من {DESCRIPTION_SOFT_LIMIT} حرفًا.",
                                  f"items.{item['row']}.description")
    rate = parse_rate(form.get("vat_rate", ""))
    totals = compute_totals(items, rate)
    if totals["total"] > MAX_AMOUNT:
        raise ValidationError("المبلغ الإجمالي كبير جدًا.", "items")
    return {
        "invoice_date": normalize_date(form.get("date", "")),
        "supplier": supplier, "buyer": dict(buyer), "items": items, "vat_rate": rate, **totals,
        "payment_method": normalize_payment(form.get("payment", "cash")),
        "serial_no": str(form.get("serial_no", "") or "").strip(),
    }


class PurchaseService(InvoiceService):
    """Reuses the sale-invoice pipeline (``_render`` / ``_publish``): the PDF and Excel come from the same code."""

    # -- paths ---------------------------------------------------------------- #
    def default_pdf_path(self, data: Dict[str, Any]) -> Path:
        d = date_to_date(data["invoice_date"])
        who = safe_filename(data["supplier"].get("name", ""), 30)
        name = safe_filename(f"فاتورة_شراء_{data['invoice_no']}_{d.isoformat()}_{who}", 90) + ".pdf"
        return unique_path(self.output_dir() / PURCHASE_FOLDER / f"{d.year}" / f"{d.month:02d}" / name)

    # -- numbering ---------------------------------------------------------- #
    def _assign_numbers(self, data: Dict[str, Any]):
        try:
            reservation = self.repo.purchases.reserve_numbers(date_to_date(data["invoice_date"]), data["supplier"],
                                                              data.get("serial_no", ""))
        except (NumberingError, ValueError) as exc:
            raise ValidationError(str(exc), "supplier.name") from exc
        data["invoice_no"], data["serial_no"] = reservation.invoice_no, reservation.serial_no
        return reservation

    # -- pipeline ------------------------------------------------------------- #
    def issue(self, data: Dict[str, Any], pdf_target: Optional[Path] = None,
              progress: Progress = None) -> PurchaseResult:
        reservation = self._assign_numbers(data)
        workdir = None
        try:
            pdf_target = Path(pdf_target) if pdf_target else self.default_pdf_path(data)
            if pdf_target.suffix.lower() != ".pdf":
                pdf_target = pdf_target.with_suffix(".pdf")
            workdir, xlsm, pdf, error = self._render(to_export_data(data), progress)
            if progress:
                progress("حفظ الملفات…")
            pdf_final, xlsm_final = self._publish(workdir, xlsm, pdf, pdf_target, data)
            warnings = []
            if pdf_final is None:
                warnings.append("تعذّر إنشاء PDF. حُفظت نسخة Excel المعبأة، ويمكنك فتحها وطباعتها أو إعادة التصدير لاحقًا.\n"
                                + (error or ""))
            try:
                purchase_id, created = self.repo.purchases.add_purchase(
                    data, str(pdf_final or xlsm_final), str(xlsm_final or ""))
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            except Exception:
                log.exception("files written but database insert failed")
                raise
        except BaseException:
            self.repo.purchases.release_numbers(reservation)
            raise
        finally:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
        return PurchaseResult(purchase_id, pdf_final, xlsm_final, warnings, created)

    def edit(self, purchase_id: int, data: Dict[str, Any], progress: Progress = None) -> PurchaseResult:
        """Replace the content of an issued purchase (its number, serial and buyer block stay) and rebuild its files."""
        old = self.repo.purchases.get_purchase(purchase_id)
        if not old:
            raise ValidationError("فاتورة الشراء غير موجودة.")
        data["invoice_no"], data["serial_no"], data["buyer"] = old["invoice_no"], old["serial_no"], old["buyer"]
        old_pdf = Path(old["file_path"]).with_suffix(".pdf") if old["file_path"] else None
        same_name = (old["invoice_date"] == data["invoice_date"]
                     and (old["supplier"].get("name") or "") == (data["supplier"].get("name") or ""))
        target = old_pdf if (old_pdf is not None and same_name) else self.default_pdf_path(data)
        workdir = None
        try:
            workdir, xlsm, pdf, error = self._render(to_export_data(data), progress)
            if progress:
                progress("حفظ الملفات…")
            pdf_final, xlsm_final = self._publish(workdir, xlsm, pdf, target, data)
        finally:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
        try:
            created = self.repo.purchases.update_purchase(purchase_id, data, str(pdf_final or xlsm_final or ""),
                                                          str(xlsm_final or ""))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        keep = {p.resolve() for p in (pdf_final, xlsm_final) if p}
        for stale in (old["file_path"], old["xlsm_path"]):
            self._remove_file(stale, keep)
        warnings = [] if pdf_final else ["تعذّر إنشاء PDF. حُفظت نسخة Excel المعبأة.\n" + (error or "")]
        return PurchaseResult(purchase_id, pdf_final, xlsm_final, warnings, created)

    def delete(self, purchase_id: int, remove_files: bool = True) -> List[Path]:  # type: ignore[override]
        record = self.repo.purchases.delete_purchase(purchase_id)
        if record is None:
            raise ValidationError("فاتورة الشراء غير موجودة.")
        removed: List[Path] = []
        if remove_files:
            for key in ("file_path", "xlsm_path"):
                gone = self._remove_file(record.get(key) or "")
                if gone:
                    removed.append(gone)
        return removed

    def reexport(self, purchase_id: int, progress: Progress = None) -> PurchaseResult:  # type: ignore[override]
        data = self.repo.purchases.get_purchase(purchase_id)
        if not data:
            raise ValidationError("فاتورة الشراء غير موجودة.")
        old = Path(data["file_path"]) if data["file_path"] else None
        target = old.with_suffix(".pdf") if old else self.default_pdf_path(data)
        workdir = None
        try:
            workdir, xlsm, pdf, error = self._render(to_export_data(data), progress)
            pdf_final, xlsm_final = self._publish(workdir, xlsm, pdf, target, data)
        finally:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
        self.repo.purchases.update_files(purchase_id, str(pdf_final or xlsm_final or ""), str(xlsm_final or ""))
        warnings = [] if pdf_final else ["تعذّر إنشاء PDF.\n" + (error or "")]
        return PurchaseResult(purchase_id, pdf_final, xlsm_final, warnings, [])
