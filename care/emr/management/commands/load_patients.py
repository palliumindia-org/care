import csv
import io
import re
from psycopg import logger
import requests
from datetime import datetime
from django.core.management.base import BaseCommand
from django.db import transaction
from django.core.paginator import Paginator

from care.emr.models import Patient
from care.users.models import User
from care.facility.models.facility import Facility
from care.emr.signals.patient.facility_name_identifier import FacilityPatientNameIdentifierConfig
from care.emr.signals.patient.phone_number_identifier import PhoneNumberIdentifierConfig


class Command(BaseCommand):
    help = "Bulk import patient data from a Google Sheet (CSV export link) and update identifiers"

    def add_arguments(self, parser):
        parser.add_argument("sheet_url", type=str, help="Google Sheet CSV export link")
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

        patients_to_create = []
        error_rows = []
        created_count = 0
        skipped_count = 0

        # regex definitions
        indian_mobile_number_regex = re.compile(r"^\+91[6-9]\d{9}$")
        international_mobile_number_regex = re.compile(r"^\+\d{1,3}\d{8,14}$")
        landline_number_regex = re.compile(r"^\+91[2-9]\d{7,9}$")
        support_number_regex = re.compile(r"^(1800|1860)\d{6,7}$")

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

        with transaction.atomic():
            for row in reader:
                try:
                    name = row.get("name", "").strip()
                    gender = row.get("gender", "").strip()
                    phone_number = (row.get("phone_number") or "").strip()
                    emergency_phone = (row.get("emergency_phone_number") or "").strip()
                    address = (row.get("address") or "").strip()
                    permanent_address = (row.get("permanent_address") or "").strip()
                    pincode = row.get("pincode")
                    dob_str = (row.get("date of birth") or "").strip()
                    age_str = (row.get("age") or "").strip()

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
                            error_rows.append({**row, "error": "Invalid phone number format"})
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
                    if not date_of_birth and age_str.isdigit():
                        year_of_birth = datetime.now().year - int(age_str)

                    # --- duplicate check ---
                    if phone_number and Patient.objects.filter(phone_number=phone_number).exists():
                        skipped_count += 1
                        continue

                    # --- create patient object ---
                    patient = Patient(
                        name=name,
                        gender=gender or "",
                        phone_number=phone_number or "",
                        emergency_phone_number=emergency_phone or "",
                        address=address,
                        permanent_address=permanent_address,
                        pincode=int(pincode) if pincode and pincode.isdigit() else None,
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
                    patients_to_create.append(patient)

                except Exception as e:
                    error_rows.append({**row, "error": str(e)})

            # --- dry run ---
            if dry_run:
                self.stdout.write(self.style.NOTICE(f"Dry run mode. {len(patients_to_create)} patients parsed."))
                for p in patients_to_create[:5]:
                    self.stdout.write(f"→ {p.name} ({p.phone_number})")
                return

            # --- create patients in bulk ---
            Patient.objects.bulk_create(patients_to_create, batch_size=1000)
            created_count = len(patients_to_create)
            self.stdout.write(self.style.SUCCESS(
                f"Imported {created_count} patients successfully. Skipped {skipped_count} duplicates."
            ))

            # --- update identifiers for all patients using pagination ---
            facility = Facility.objects.first()
            if not facility:
                self.stdout.write(self.style.WARNING("⚠️ No facility found. Skipping identifier updates."))
                return

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
                        PhoneNumberIdentifierConfig.update_identifier(patient)
                        patient.build_instance_identifiers()
                        patient.build_facility_identifiers(facility.id)
                        updated_patients.append(patient)

                    except Exception as e:
                        self.stdout.write(self.style.WARNING(
                            f"Identifier update failed for {patient.name}: {e}"
                        ))

                Patient.objects.bulk_update(updated_patients, ["instance_identifiers", "facility_identifiers"], batch_size=1000)
                self.stdout.write(self.style.SUCCESS(
                    f"✅ Batch {page_number}/{total_pages} completed - updated {len(updated_patients)} patients"
                ))
                total_updated_count += len(updated_patients)

            self.stdout.write(self.style.SUCCESS("✅ Identifier update completed successfully."))
            self.stdout.write(self.style.SUCCESS(f"Total patients updated: {total_updated_count}"))

            # # --- error report ---
            # if error_rows:
            #     self.stdout.write(self.style.WARNING(f"{len(error_rows)} rows had errors."))
            #     for err in error_rows[:5]:
            #         self.stdout.write(f"⚠️ {err.get('name', 'Unknown')} — {err['error']}")
