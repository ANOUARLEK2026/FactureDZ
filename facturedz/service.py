# -*- coding: utf-8 -*-
"""Business logic with no GUI code: validation, numbering, issuing and re-exporting invoices."""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .amounts import (MAX_AMOUNT, line_total, parse_decimal, q2, to_decimal,
                      vat_amount)
from .config import MAX_ITEMS, NUMBER_MAX_DIGITS, PAYMENT_ALIASES, PAYMENT_METHODS
from .db import DatabaseError, Repository
from .numbering import MIN_INVOICE_DIGITS, NumberingError
from .paths import template_path
from .pdf_invoice import render_invoice_pdf
from .template import TemplateError, fill_template, validate_template
from .textnorm import normalize_ar, safe_filename, to_ascii_digits

log = logging.getLogger("facturedz.service")
Progress = Optional[Callable[[str], None]]


class ValidationError(ValueError):
    """User-facing (Arabic) validation problem. ``field`` tells the UI what to focus."""

    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.field = field


# --------------------------------------------------------------------------- #
# validation / calculation                                                    #
# --------------------------------------------------------------------------- #
def normalize_date(text: str) -> str:
    """Accept d.m.y, d/m/y, d-m-y, y-m-d (any digits) -> 'dd.mm.yyyy'."""
    s = to_ascii_digits(text).strip()
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%y"):
        try:
            d = dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        if not 2000 <= d.year <= 2100:
            break
        return d.strftime("%d.%m.%Y")
    raise ValidationError("التاريخ غير صحيح. اكتبه هكذا: 10.10.2026", "date")


def date_to_date(text: str) -> dt.date:
    return dt.datetime.strptime(text, "%d.%m.%Y").date()


def _number_text(text: str, field_name: str, label: str) -> str:
    """Digits only, 1..NUMBER_MAX_DIGITS long. Long numbers keep their leading zero (an invoice number starts
    with the month: "0126..." is January 2026, so it must never be turned into an int and back)."""
    s = to_ascii_digits(text).strip()
    if not re.fullmatch(rf"\d{{1,{NUMBER_MAX_DIGITS}}}", s) or int(s) < 1:
        raise ValidationError(f"{label} يجب أن يكون رقمًا صحيحًا موجبًا (حتى {NUMBER_MAX_DIGITS} رقمًا).", field_name)
    return s if len(s) >= MIN_INVOICE_DIGITS else str(int(s))


def normalize_payment(value: str) -> str:
    value = PAYMENT_ALIASES.get(value, value)
    return value if value in dict(PAYMENT_METHODS) else "cash"


def compute_items(raw_items: List[Dict[str, str]], *, strict: bool):
    """Parse raw rows. strict=False (live typing) skips bad rows; strict=True raises ValidationError."""
    items: List[Dict[str, Any]] = []
    for index, raw in enumerate(raw_items, start=1):
        desc = str(raw.get("description", "") or "").strip()
        raw_q, raw_p = str(raw.get("quantity", "") or "").strip(), str(raw.get("price", "") or "").strip()
        if not (desc or raw_q or raw_p):
            continue
        try:
            qty, price = parse_decimal(raw_q), parse_decimal(raw_p)
        except ValueError:
            if strict:
                raise ValidationError(f"تحقق من الكمية والسعر في السطر {index}.", f"items.{index}.quantity")
            continue
        if strict:
            if not desc:
                raise ValidationError(f"اكتب اسم السلعة في السطر {index}.", f"items.{index}.description")
            if qty <= 0:
                raise ValidationError(f"الكمية في السطر {index} يجب أن تكون أكبر من صفر.", f"items.{index}.quantity")
            if price < 0:
                raise ValidationError(f"السعر في السطر {index} لا يمكن أن يكون سالبًا.", f"items.{index}.price")
        elif qty < 0 or price < 0:
            continue
        item = {"description": desc, "quantity": qty, "unit_price": price,
                "line_total": line_total(qty, price), "row": index}
        pid = raw.get("product_id")
        if pid not in (None, ""):                      # link to the stock catalogue (set by the item table)
            try:
                item["product_id"] = int(pid)
            except (TypeError, ValueError):
                pass
        items.append(item)
    return items


def compute_totals(items: List[Dict[str, Any]], rate: Decimal) -> Dict[str, Decimal]:
    subtotal = q2(sum((i["line_total"] for i in items), Decimal(0)))
    vat = vat_amount(subtotal, rate)
    return {"subtotal": subtotal, "vat_amount": vat, "total": q2(subtotal + vat)}


def parse_rate(text: str) -> Decimal:
    try:
        rate = parse_decimal(text)
    except ValueError:
        raise ValidationError("نسبة TVA غير صحيحة.", "vat_rate")
    if not Decimal(0) <= rate <= Decimal(100):
        raise ValidationError("نسبة TVA يجب أن تكون بين 0 و100.", "vat_rate")
    return rate


def build_invoice(form: Dict[str, Any], seller: Dict[str, str]) -> Dict[str, Any]:
    """Validate everything and return the canonical invoice dict used by export + database."""
    customer = {k: str(v or "").strip() for k, v in form.get("customer", {}).items()}
    if not customer.get("name"):
        raise ValidationError("أدخل اسم المشتري أو اختر ملفًا محفوظًا.", "customer.name")
    items = compute_items(form.get("items", []), strict=True)
    if not items:
        raise ValidationError("أضف سلعة واحدة على الأقل مع الكمية والسعر.", "items.1.description")
    if len(items) > MAX_ITEMS:
        raise ValidationError(f"الحد الأقصى {MAX_ITEMS} سلعة في الفاتورة الواحدة.", "items")
    rate = parse_rate(form.get("vat_rate", ""))
    totals = compute_totals(items, rate)
    if totals["total"] > MAX_AMOUNT:
        raise ValidationError("المبلغ الإجمالي كبير جدًا.", "items")
    data = {
        "invoice_date": normalize_date(form.get("date", "")),
        "customer": customer, "seller": dict(seller), "items": items,
        "vat_rate": rate, **totals,
        "payment_method": normalize_payment(form.get("payment", "cash")),
    }
    # Numbers are normally handed out by InvoiceService.issue(); a form may still carry them (editing an issued
    # invoice, or the serial that was shown on screen), in which case they are validated here.
    if str(form.get("invoice_no", "") or "").strip():
        data["invoice_no"] = _number_text(form["invoice_no"], "invoice_no", "رقم الفاتورة")
    if str(form.get("serial_no", "") or "").strip():
        data["serial_no"] = _number_text(form["serial_no"], "serial_no", "الرقم التسلسلي")
    return data


# --------------------------------------------------------------------------- #
# issuing                                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class IssueResult:
    invoice_id: int
    pdf_path: Optional[Path]
    xlsm_path: Optional[Path]
    warnings: List[str] = field(default_factory=list)

    @property
    def pdf_ok(self) -> bool:
        return self.pdf_path is not None


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 1000):
        cand = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not cand.exists():
            return cand
    raise OSError("تعذر إيجاد اسم ملف غير مستخدم.")


def atomic_copy(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
    return dst


class InvoiceService:
    def __init__(self, repo: Repository, template: Optional[Path] = None):
        self.repo = repo
        self.template = Path(template) if template else template_path()

    # -- settings helpers ------------------------------------------------ #
    def output_dir(self) -> Path:
        return Path(self.repo.get_setting("output_dir", str(self.repo.default_output)) or self.repo.default_output)

    def keep_xlsm(self) -> bool:
        return self.repo.get_setting("keep_xlsm", "1") == "1"

    def default_pdf_path(self, data: Dict[str, Any]) -> Path:
        d = date_to_date(data["invoice_date"])
        who = safe_filename(data["customer"].get("name", ""), 30)
        name = safe_filename(f"فاتورة_{data['invoice_no']}_{d.isoformat()}_{who}", 90) + ".pdf"
        return unique_path(self.output_dir() / f"{d.year}" / f"{d.month:02d}" / name)

    # -- pipeline -------------------------------------------------------- #
    def _render(self, data: Dict[str, Any], progress: Progress):
        """Fill the Excel template and draw the PDF from the same data.
        Returns (workdir, xlsm, pdf | None, error | None). The Excel file is always produced."""
        workdir = Path(tempfile.mkdtemp(prefix="fdz_"))
        if progress:
            progress("تعبئة قالب Excel…")
        xlsm = fill_template(self.template, workdir / "invoice.xlsm", data)
        if progress:
            progress("إنشاء ملف PDF…")
        pdf: Optional[Path] = None
        error: Optional[str] = None
        try:
            pdf = render_invoice_pdf(data, workdir / "invoice.pdf", self.template)
        except Exception as exc:  # noqa: BLE001 - the invoice must still be saved as Excel
            log.exception("PDF rendering failed")
            error = f"{type(exc).__name__}: {exc}"
        return workdir, xlsm, pdf, error

    def _publish(self, workdir: Path, xlsm: Path, pdf: Optional[Path], pdf_target: Path, data: Dict[str, Any]):
        """Move results to their final place. Always keeps the xlsm if there is no PDF."""
        xlsm_final: Optional[Path] = None
        if self.keep_xlsm() or pdf is None:
            xlsm_final = atomic_copy(xlsm, pdf_target.with_suffix(".xlsm"))
        pdf_final = atomic_copy(pdf, pdf_target) if pdf is not None else None
        return pdf_final, xlsm_final

    def _assign_numbers(self, data: Dict[str, Any]):
        """Give a new invoice its numbers. Returns the reservation to release if issuing fails (None if the
        caller supplied both numbers, which are then only checked for duplicates)."""
        if data.get("invoice_no"):
            if not data.get("serial_no"):
                raise ValidationError("الرقم التسلسلي مطلوب مع رقم الفاتورة.", "serial_no")
            conflict = self.repo.duplicate_of(data["invoice_no"], data["serial_no"])
            if conflict:
                what = "رقم الفاتورة" if conflict == "invoice" else "الرقم التسلسلي"
                raise ValidationError(f"{what} مستخدم مسبقًا (ولو في فاتورة محذوفة).", f"{conflict}_no")
            return None
        try:
            reservation = self.repo.reserve_numbers(date_to_date(data["invoice_date"]), data["customer"],
                                                    data.get("serial_no", ""))
        except (NumberingError, ValueError) as exc:
            raise ValidationError(str(exc), "customer.name") from exc
        except DatabaseError as exc:
            raise ValidationError(str(exc)) from exc
        data["invoice_no"], data["serial_no"] = reservation.invoice_no, reservation.serial_no
        return reservation

    def issue(self, data: Dict[str, Any], pdf_target: Optional[Path] = None, progress: Progress = None) -> IssueResult:
        reservation = self._assign_numbers(data)
        workdir = None
        try:
            pdf_target = Path(pdf_target) if pdf_target else self.default_pdf_path(data)
            if pdf_target.suffix.lower() != ".pdf":
                pdf_target = pdf_target.with_suffix(".pdf")
            workdir, xlsm, pdf, error = self._render(data, progress)
            if progress:
                progress("حفظ الملفات…")
            pdf_final, xlsm_final = self._publish(workdir, xlsm, pdf, pdf_target, data)
            warnings = []
            if pdf_final is None:
                warnings.append("تعذّر إنشاء PDF. حُفظت نسخة Excel المعبأة، ويمكنك فتحها وطباعتها أو إعادة التصدير لاحقًا.\n"
                                + (error or ""))
            try:
                invoice_id = self.repo.add_invoice(data, str(pdf_final or xlsm_final), str(xlsm_final or ""))
            except Exception:
                log.exception("files written but database insert failed")
                raise
        except BaseException:
            if reservation is not None:
                self.repo.release_numbers(reservation)
            raise
        finally:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
        return IssueResult(invoice_id, pdf_final, xlsm_final, warnings)

    # -- editing / deleting issued invoices ----------------------------------- #
    def edit(self, invoice_id: int, data: Dict[str, Any], progress: Progress = None) -> IssueResult:
        """Replace the content of an issued invoice and rebuild its PDF/Excel.

        The invoice number, the serial and the seller block are those of the original and cannot be changed here.
        """
        old = self.repo.get_invoice(invoice_id)
        if not old:
            raise ValidationError("الفاتورة غير موجودة.")
        data["invoice_no"], data["serial_no"], data["seller"] = old["invoice_no"], old["serial_no"], old["seller"]
        old_pdf = Path(old["file_path"]).with_suffix(".pdf") if old["file_path"] else None
        same_name = (old["invoice_date"] == data["invoice_date"]
                     and (old["customer"].get("name") or "") == (data["customer"].get("name") or ""))
        target = old_pdf if (old_pdf is not None and same_name) else self.default_pdf_path(data)

        workdir = None
        try:
            workdir, xlsm, pdf, error = self._render(data, progress)
            if progress:
                progress("حفظ الملفات…")
            pdf_final, xlsm_final = self._publish(workdir, xlsm, pdf, target, data)
        finally:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
        try:
            self.repo.update_invoice(invoice_id, data, str(pdf_final or xlsm_final or ""), str(xlsm_final or ""))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        keep = {p.resolve() for p in (pdf_final, xlsm_final) if p}
        for stale in (old["file_path"], old["xlsm_path"]):          # files left behind by a rename / format change
            self._remove_file(stale, keep)
        warnings = [] if pdf_final else ["تعذّر إنشاء PDF. حُفظت نسخة Excel المعبأة.\n" + (error or "")]
        return IssueResult(invoice_id, pdf_final, xlsm_final, warnings)

    @staticmethod
    def _remove_file(path_text: str, keep=()) -> Optional[Path]:
        """Delete an invoice file (only .pdf / .xlsm, never something the database merely points at)."""
        if not path_text:
            return None
        path = Path(path_text)
        if path.suffix.lower() not in (".pdf", ".xlsm"):
            return None
        try:
            if path.is_file() and path.resolve() not in keep:
                path.unlink()
                return path
        except OSError:
            log.exception("could not delete %s", path)
        return None

    def delete(self, invoice_id: int, remove_files: bool = True) -> List[Path]:
        """Delete an invoice record; its numbers are never reused. Returns the files that were removed."""
        record = self.repo.delete_invoice(invoice_id)
        if record is None:
            raise ValidationError("الفاتورة غير موجودة.")
        removed: List[Path] = []
        if remove_files:
            for key in ("file_path", "xlsm_path"):
                gone = self._remove_file(record.get(key) or "")
                if gone:
                    removed.append(gone)
        return removed

    def reexport(self, invoice_id: int, progress: Progress = None) -> IssueResult:
        data = self.repo.get_invoice(invoice_id)
        if not data:
            raise ValidationError("الفاتورة غير موجودة.")
        old = Path(data["file_path"]) if data["file_path"] else None
        target = old.with_suffix(".pdf") if old else self.default_pdf_path(data)
        workdir = None
        try:
            workdir, xlsm, pdf, error = self._render(data, progress)
            pdf_final, xlsm_final = self._publish(workdir, xlsm, pdf, target, data)
        finally:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
        self.repo.update_invoice_files(invoice_id, str(pdf_final or xlsm_final or ""), str(xlsm_final or ""))
        warnings = [] if pdf_final else ["تعذّر إنشاء PDF.\n" + (error or "")]
        return IssueResult(invoice_id, pdf_final, xlsm_final, warnings)

    # -- diagnostics ------------------------------------------------------ #
    def diagnostics(self) -> List[tuple]:
        """[(label, ok, detail)] shown on the Settings page to make support easy."""
        rows = []
        problems = validate_template(self.template)
        rows.append(("قالب Excel", not problems, "سليم" if not problems else "؛ ".join(problems)))
        try:
            from PySide6 import __version__ as qt_version
            from PySide6.QtGui import QPdfWriter  # noqa: F401
            rows.append(("محرّك PDF (Qt)", True, f"جاهز — PySide6 {qt_version}"))
        except ImportError as exc:
            rows.append(("محرّك PDF (Qt)", False, f"PySide6 غير مثبت: {exc}"))
        issue = self.repo.integrity_problem()
        rows.append(("سلامة قاعدة البيانات", not issue, "سليمة" if not issue else issue))
        try:
            stock = self.repo.stock.summary()
            rows.append(("المخزون", True, f"{stock['count']} صنف • {stock['movements']} حركة"))
        except Exception as exc:  # noqa: BLE001
            rows.append(("المخزون", False, str(exc)))
        try:
            probe = self.output_dir()
            probe.mkdir(parents=True, exist_ok=True)
            test = probe / ".write_test"
            test.write_text("ok")
            test.unlink()
            rows.append(("مجلد الفواتير", True, str(probe)))
        except OSError as exc:
            rows.append(("مجلد الفواتير", False, f"لا يمكن الكتابة: {exc}"))
        return rows
