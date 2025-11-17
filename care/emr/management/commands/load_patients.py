import csv
import io
import re
from datetime import datetime

import requests
from django.core.management.base import BaseCommand
from django.db import transaction
from django.core.paginator import Paginator

from care.emr.models import (
    Patient,
    PatientIdentifier,
    PatientIdentifierConfig,
)
from care.users.models import User
from care.facility.models.facility import Facility
from care.emr.signals.patient.facility_name_identifier import FacilityPatientNameIdentifierConfig
from care.emr.signals.patient.phone_number_identifier import PhoneNumberIdentifierConfig


class Command(BaseCommand):
    help = "Bulk import patient data from a Google Sheet (CSV export link) and add Pallium ID identifiers"

    def add_arguments(self, parser):
        parser.add_argument("sheet_url", type=str, help="Google Sheet CSV export link (CSV export URL)")
        parser.add_argument("--dry-run", action="store_true", help="Preview records without saving")

    def handle(self, *args, **options):
        sheet_url = options["sheet_url"]
        dry_run = options["dry_run"]

        self.stdout.write(self.style.NOTICE(f"Fetching data from: {sheet_url}"))
        response = requests.get(sheet_url)
        response.raise_for_status()

        csv_text = response.text
        csvfile = io.StringIO(csv_text)
        reader = csv.DictReader(csvfile)

        patients_to_create = []  # list of tuples (Patient instance, pallium_id, original_row)
        error_rows = []
        created_count = 0
        skipped_count = 0

        # regex definitions
        indian_mobile_number_regex = re.compile(r"^\+91[6-9]\d{9}$")
        international_mobile_number_regex = re.compile(r"^\+\d{1,3}\d{8,14}$")
        landline_number_regex = re.compile(r"^\+91[2-9]\d{7,9}$")
        support_number_regex = re.compile(r"^(1800|1860)\d{6,7}$")

        # Pallium ID regex (must start with 1-9, optionally followed by digits)
        pallium_regex = re.compile(r"^[1-9]\d*$")

        # get or create care user
        care_user, _ = User.objects.get_or_create(
            username="careuser",
            defaults={
                "first_name": "Care",
                "last_name": "User",
                "user_type": "care_user",
                "email": "careuser@ohc.network",
                "phone_number": "",
                "is_active": False,
            }
        )

        # Ensure facility exists (we need a facility to attach PatientIdentifier)
        facility = Facility.objects.first()
        if not facility:
            self.stdout.write(self.style.WARNING("⚠️ No facility found. Aborting import."))
            return

        # Ensure / update the PatientIdentifierConfig for pallium_id
        pallium_config, created = PatientIdentifierConfig.objects.get_or_create(
            facility=None,
            config__system="system.care.ohc.network/pallium_id",
            defaults={
                "status": "active",
                "config": {
                    "use": "official",
                    "description": "Pallium patient identifier",
                    "system": "system.care.ohc.network/pallium_id",
                    "required": True,
                    "unique": True,
                    "regex": r"^[1-9]\d*$",
                    "display": "Pallium ID",
                    "auto_maintained": False,
                    "created_by": care_user.id,
                    "updated_by": care_user.id,
                }
            }
        )

        phone_config, _ = PatientIdentifierConfig.objects.get_or_create(
            facility=None,
            config__system="system.care.ohc.network/patient-phone-number",
            defaults={
                "status": "active",
                "config": {
                    "use": "official",
                    "description": "Patient phone number identifier",
                    "system": "system.care.ohc.network/patient-phone-number",
                    "required": False,
                    "unique": True,
                    "regex": r"^\+\d{1,3}\d{8,14}$",
                    "display": "Phone Number",
                    "auto_maintained": False,
                    "created_by": care_user.id,
                    "updated_by": care_user.id,
                }
            }
        )


        # Start transaction for safety
        with transaction.atomic():
            for row_idx, row in enumerate(reader, start=1):
                try:
                    name = (row.get("name") or "").strip()
                    gender = (row.get("gender") or "").strip()
                    phone_number = (row.get("phone_number") or "").strip()
                    emergency_phone = (row.get("emergency_phone_number") or "").strip()
                    address = (row.get("address") or "").strip()
                    permanent_address = (row.get("permanent_address") or "").strip()
                    pincode = row.get("pincode")
                    dob_str = (row.get("date of birth") or "").strip()
                    age_str = (row.get("age") or "").strip()

                    # --- Pallium ID (required) ---
                    # NOTE: CSV column assumed to be "pallium_id". If your column is different, change this key.
                    pallium_id = (row.get("pallium_id") or "").strip()
                    if not pallium_id:
                        error_rows.append({**row, "error": "Missing Pallium ID", "row": row_idx})
                        continue
                    if not pallium_regex.match(pallium_id):
                        error_rows.append({**row, "error": f"Invalid Pallium ID: {pallium_id}", "row": row_idx})
                        continue

                    # --- normalize and validate phone numbers ---
                    if phone_number:
                        if len(phone_number) == 10 and phone_number.isdigit():
                            phone_number = "+91" + phone_number

                        valid = (
                            indian_mobile_number_regex.match(phone_number)
                            or international_mobile_number_regex.match(phone_number)
                            or landline_number_regex.match(phone_number)
                            or support_number_regex.match(phone_number)
                        )
                        if not valid:
                            error_rows.append({**row, "error": "Invalid phone number format", "row": row_idx})
                            continue

                    # --- parse date of birth or year of birth ---
                    date_of_birth = None
                    year_of_birth = None
                    if dob_str:
                        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
                            try:
                                date_of_birth = datetime.strptime(dob_str, fmt).date()
                                year_of_birth = date_of_birth.year
                                break
                            except ValueError:
                                continue
                    if not date_of_birth and age_str and age_str.isdigit():
                        year_of_birth = datetime.now().year - int(age_str)

                    # --- duplicate check by phone_number (if present) ---
                    if phone_number and Patient.objects.filter(phone_number=phone_number).exists():
                        skipped_count += 1
                        continue

                    # --- create patient object (unsaved) ---
                    patient = Patient(
                        name=name,
                        gender=gender or "",
                        phone_number=phone_number or "",
                        emergency_phone_number=emergency_phone or "",
                        address=address,
                        permanent_address=permanent_address,
                        pincode=int(pincode) if pincode and str(pincode).isdigit() else None,
                        date_of_birth=date_of_birth,
                        year_of_birth=year_of_birth,
                        marital_status="",
                        blood_group="",
                        geo_organization=None,
                        organization_cache=[],
                        users_cache=[care_user.id],
                        instance_identifiers=[],
                        facility_identifiers={},
                        instance_tags=[],
                        facility_tags={},
                        created_by=care_user,
                        updated_by=care_user,
                    )

                    patients_to_create.append((patient, pallium_id, row))

                except Exception as e:
                    error_rows.append({**row, "error": str(e), "row": row_idx})

            # --- dry run ---
            if dry_run:
                self.stdout.write(self.style.NOTICE(f"Dry run mode. {len(patients_to_create)} patients parsed."))
                for p, pid, _ in patients_to_create[:10]:
                    self.stdout.write(f"→ {p.name} ({p.phone_number}) — Pallium ID: {pid}")
                # show a few errors
                if error_rows:
                    self.stdout.write(self.style.WARNING(f"{len(error_rows)} rows had errors (showing up to 5):"))
                    for err in error_rows[:5]:
                        self.stdout.write(f"⚠️ Row {err.get('row', '?')}: {err.get('error')}")
                return

            # --- bulk create patients ---
            patients = [t[0] for t in patients_to_create]
            pallium_values = [t[1] for t in patients_to_create]
            original_rows = [t[2] for t in patients_to_create]

            if patients:
                Patient.objects.bulk_create(patients, batch_size=1000)
                created_count = len(patients)
                self.stdout.write(self.style.SUCCESS(
                    f"Imported {created_count} patients successfully. Skipped {skipped_count} duplicates."
                ))
            else:
                self.stdout.write(self.style.NOTICE("No patients to create."))
                # still continue to possibly report errors
            # Note: on Postgres, bulk_create will populate PKs on 'patients' list items.

            # --- create PatientIdentifier objects for Pallium ID ---
            patient_identifiers = []
            pid_error_count = 0
            for patient, pallium_value, orig_row in zip(patients, pallium_values, original_rows):
                try:
                    # double-check the pallium uniqueness - if exists, report error
                    exists = PatientIdentifier.objects.filter(config=pallium_config, value=pallium_value).exists()
                    if exists:
                        error_rows.append({**orig_row, "error": f"Pallium ID already exists: {pallium_value}"})
                        pid_error_count += 1
                        continue

                    pi = PatientIdentifier(
                        patient=patient,
                        config=pallium_config,
                        value=pallium_value
                    )
                    patient_identifiers.append(pi)

                except Exception as e:
                    error_rows.append({**orig_row, "error": f"Failed to prepare identifier: {e}"})
                    pid_error_count += 1

            if patient_identifiers:
                PatientIdentifier.objects.bulk_create(patient_identifiers, batch_size=1000)
                self.stdout.write(self.style.SUCCESS(
                    f"Created {len(patient_identifiers)} Pallium ID identifiers."
                ))
            else:
                if pid_error_count:
                    self.stdout.write(self.style.WARNING(f"No Pallium identifiers created due to {pid_error_count} errors."))
                else:
                    self.stdout.write(self.style.NOTICE("No Pallium identifiers to create."))

            # --- create PatientIdentifier objects for Phone Number ---
            phone_identifiers = []
            phone_error_count = 0

            for patient, orig_row in zip(patients, original_rows):
                try:
                    phone_value = patient.phone_number.strip() if patient.phone_number else ""

                    # skip if no phone number
                    if not phone_value:
                        continue

                    # uniqueness check
                    exists = PatientIdentifier.objects.filter(
                        config=phone_config,
                        value=phone_value
                    ).exists()

                    if exists:
                        error_rows.append({
                            **orig_row,
                            "error": f"Phone number identifier already exists: {phone_value}"
                        })
                        phone_error_count += 1
                        continue

                    pi = PatientIdentifier(
                        patient=patient,
                        config=phone_config,
                        value=phone_value
                    )
                    phone_identifiers.append(pi)

                except Exception as e:
                    error_rows.append({**orig_row, "error": f"Failed to prepare phone identifier: {e}"})
                    phone_error_count += 1

            if phone_identifiers:
                PatientIdentifier.objects.bulk_create(phone_identifiers, batch_size=1000)
                self.stdout.write(self.style.SUCCESS(
                    f"Created {len(phone_identifiers)} phone number identifiers."
                ))
            else:
                if phone_error_count:
                    self.stdout.write(self.style.WARNING(
                        f"No phone number identifiers created due to {phone_error_count} errors."
                    ))
                else:
                    self.stdout.write(self.style.NOTICE("No phone number identifiers to create."))


            # --- update identifiers for all patients using pagination (your existing logic) ---
            paginator = Paginator(Patient.objects.all().order_by("id"), 5000)
            total_pages = paginator.num_pages
            total_updated_count = 0

            self.stdout.write(self.style.NOTICE(
                f"Updating identifiers in {total_pages} batches of 5000..."
            ))

            for page_number in paginator.page_range:
                page = paginator.page(page_number)
                updated_patients = []

                self.stdout.write(self.style.NOTICE(
                    f"Processing batch {page_number}/{total_pages} ({len(page.object_list)} patients)..."
                ))
                for patient in page.object_list:
                    try:
                        FacilityPatientNameIdentifierConfig.update_identifier(patient, facility)
                        patient.build_instance_identifiers()
                        patient.build_facility_identifiers(facility.id)
                        updated_patients.append(patient)

                    except Exception as e:
                        self.stdout.write(self.style.WARNING(
                            f"Identifier update failed for {patient.name}: {e}"
                        ))

                if updated_patients:
                    Patient.objects.bulk_update(updated_patients, ["instance_identifiers", "facility_identifiers"], batch_size=1000)
                self.stdout.write(self.style.SUCCESS(
                    f"✅ Batch {page_number}/{total_pages} completed - updated {len(updated_patients)} patients"
                ))
                total_updated_count += len(updated_patients)

            self.stdout.write(self.style.SUCCESS("✅ Identifier update completed successfully."))
            self.stdout.write(self.style.SUCCESS(f"Total patients updated: {total_updated_count}"))

            # --- error report ---
            if error_rows:
                self.stdout.write(self.style.WARNING(f"{len(error_rows)} rows had errors. Showing up to 20:"))
                for err in error_rows[:20]:
                    rowinfo = f"Row {err.get('row', '?')}"
                    name = err.get("name") or err.get("Name") or ""
                    self.stdout.write(f"⚠️ {rowinfo} — {name} — {err.get('error')}")

