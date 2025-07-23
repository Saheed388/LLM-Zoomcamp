import asyncio
import csv
import os
import re
import json
import argparse
from playwright.async_api import async_playwright
from datetime import datetime
from time import time
import random
from typing import Set, Dict, List, Optional

class ScraperError(Exception):
    pass

class TransientError(ScraperError):
    """Network issues, timeouts, temporary blocks"""
    pass

class StructuralError(ScraperError):
    """Page structure changes, missing elements"""
    pass

class PermanentError(ScraperError):
    """Invalid URLs, terminated notices, access denied"""
    pass

class ProgressTracker:
    def __init__(self, progress_file: str = "sam_gov_scraper_progress.json"):
        self.progress_file = progress_file
        self.progress_data = self._load_progress()

    def _load_progress(self) -> Dict:
        """Load progress from JSON file"""
        if os.path.exists(self.progress_file):
            try:
                with open(self.progress_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                pass

        return {
            "phase1": {
                "completed_states": [],
                "extracted_url_ids": [],
                "newly_extracted_url_ids": [],
                "last_completed_state_index": -1
            },
            "phase2": {
                "processed_url_ids": [],
                "failed_url_ids": []
            },
            "timestamps": {
                "last_phase1_run": None,
                "last_phase2_run": None
            }
        }
    def add_phase1_failure(self, state, error):
        if "phase1_failures" not in self.progress_data:
            self.progress_data["phase1_failures"] = []
        self.progress_data["phase1_failures"].append({
            "state": state,
            "error": str(error),
            "timestamp": datetime.now().isoformat()
        })
        self.save_progress()

    def add_phase2_failure(self, notice_id, error):
        if "phase2_failures" not in self.progress_data:
            self.progress_data["phase2_failures"] = []
        self.progress_data["phase2_failures"].append({
            "notice_id": notice_id,
            "error": str(error),
            "timestamp": datetime.now().isoformat()
        })
        self.save_progress()

    def save_progress(self):
        """Save progress to JSON file"""
        with open(self.progress_file, 'w', encoding='utf-8') as f:
            json.dump(self.progress_data, f, indent=2)

    def get_extracted_url_ids(self) -> List[Dict]:
        """Get set of already extracted notice IDs"""
        return self.progress_data["phase1"]["extracted_url_ids"]

    def get_newly_extracted_url_ids(self) -> List[Dict]:
        """Get set of already extracted notice IDs"""
        return self.progress_data["phase1"]["newly_extracted_url_ids"]

    def get_processed_notice_ids(self) -> Set[str]:
        """Get set of already processed notice IDs from phase 2"""
        return set(self.progress_data["phase2"]["processed_url_ids"])

    def add_extracted_url_ids(self, notice_ids: List[Dict[str,str]]):
        """Add newly extracted notice IDs"""
        existing_ids = self.progress_data["phase1"]["extracted_url_ids"]
        ex_ids = [e_id["url_id"] for e_id in existing_ids]
        new_ids = []
        for notice in notice_ids:
            if notice['url_id'] not in ex_ids:
                # self._extracted_ids_set.add(notice['url_id'])
                new_ids.append(notice)
        self.progress_data["phase1"]["extracted_url_ids"].extend(new_ids)
        self.save_progress()

    def add_newly_extracted_url_ids(self, notice_ids: List[Dict[str,str]]):
        """Add newly extracted notice IDs"""
        existing_ids = self.progress_data["phase1"]["newly_extracted_url_ids"]
        ex_ids = [e_id["url_id"] for e_id in existing_ids]
        # new_ids = [nid for nid in notice_ids if nid['url_id'] not in ex_ids]
        new_ids = []
        for notice in notice_ids:
            if notice['url_id'] not in ex_ids:
                # self._extracted_ids_set.add(notice['url_id'])
                new_ids.append(notice)
        self.progress_data["phase1"]["newly_extracted_url_ids"].extend(new_ids)
        self.save_progress()

    def add_processed_notice_id(self, notice_id: str):
        """Add processed notice ID from phase 2"""
        if notice_id not in self.progress_data["phase2"]["processed_url_ids"]:
            self.progress_data["phase2"]["processed_url_ids"].append(notice_id)
            self.save_progress()

    def mark_state_completed(self, state_index: int, state_name: str):
        """Mark a state as completed"""
        self.progress_data["phase1"]["last_completed_state_index"] = state_index
        if state_name not in self.progress_data["phase1"]["completed_states"]:
            self.progress_data["phase1"]["completed_states"].append(state_name)
        self.save_progress()

    def get_last_completed_state_index(self) -> int:
        """Get the last completed state index"""
        return self.progress_data["phase1"]["last_completed_state_index"]

    def update_timestamp(self, phase: str):
        """Update timestamp for phase completion"""
        self.progress_data["timestamps"][f"last_{phase}_run"] = datetime.now().isoformat()
        self.save_progress()

class CombinedScraper:
    def __init__(self):
        self.progress_tracker = ProgressTracker()
        self.PHASE2_OUTPUT_CSV = "sam_gov_raw_extracted_data.csv"

        # Phase 1 settings
        self.BATCH_SIZE = 25
        self.REQUEST_DELAY = 1
        self.MAX_RETRIES = 5
        self.fresh_re_run = False
        self.RETRY_DELAY = 5
        self.MAX_NONE_RESPONSES = 3
        self._max_pages = 1

        # Phase 2 settings
        self.MAX_CONCURRENT = 10
        self.PHASE2_BATCH_SIZE = 10

        # Network retry settings
        self.NETWORK_TIMEOUT = 60000
        self.NETWORK_RETRIES = 3
        self.NETWORK_RETRY_DELAY = 10

        self.states = {
            'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
             'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
            'DC': 'District of Columbia', 'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii',
            'ID': 'Idaho', 'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa',
            'KS': 'Kansas', 'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine',
            'MD': 'Maryland', 'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota',
            'MS': 'Mississippi', 'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska',
            'NV': 'Nevada', 'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico',
            'NY': 'New York', 'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio',
            'OK': 'Oklahoma', 'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island',
            'SC': 'South Carolina', 'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas',
            'UT': 'Utah', 'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington',
            'WV': 'West Virginia', 'WI': 'Wisconsin', 'WY': 'Wyoming'
        }

        base_url = "https://sam.gov/search/?page=1&pageSize=25&sort=-modifiedDate&index=opp&sfm%5BsimpleSearch%5D%5BkeywordRadio%5D=EXACT&sfm%5Bstatus%5D%5Bis_active%5D=true"

        self.URLS = [
            f"{base_url}&sfm%5BplaceOfPerformance%5D%5Bstate%5D%5B0%5D%5Bkey%5D={code}&sfm%5BplaceOfPerformance%5D%5Bstate%5D%5B0%5D%5Bvalue%5D={code}%20-%20{name.replace(' ', '%20')}&sfm%5BplaceOfPerformance%5D%5Bcountry%5D%5B0%5D%5Bkey%5D=USA&sfm%5BplaceOfPerformance%5D%5Bcountry%5D%5B0%5D%5Bvalue%5D=UNITED%20STATES"
            for code, name in self.states.items()
        ]

        # Phase 2 selectors
        self.selectors = {
            "notice_id": '//*[@id="header-solicitation-number"]/div/div[2]',
            "title": '//*[@id="main-container"]/ng-component/page/div/div/div[3]/div[2]/div[1]/h1',
            "description": '//*[@id="description"]/div[1]',
            "naics": '//*[@id="classification-naics-code"]/ul/li',
            "value": '//*[@id="award-amount"]',
            "due_date": "li#general-original-response-date:has-text('Original Date Offers Due:')",
            "end_date": '//*[@id="general-original-archive-date"]',
            "contact_name": '//*[@id="contact-primary-poc-full-name"]',
            "contact_email": '//*[@id="contact-primary-poc-email"]/a',
            "contact_phone": '//*[@id="contact-primary-poc-phone"]',
            "sec_contact_name": '//*[@id="contact-secondary-poc-full-name"]',
            "sec_contact_email": '//*[@id="contact-secondary-poc-email"]/a',
            "sec_contact_phone": '//*[@id="contact-secondary-poc-phone"]',
            "address": '//*[@id="contracting-office-contracting-office-street"]',
            "city_address": '//*[@id="contracting-office-city-contracting-office-state"]',
            "department_ind_agency": "div.header:text-is('Department/Ind. Agency') + div.description",
            "sub_tier": "div.header:text-is('Sub-tier') + div.description",
            "office": "div.header:text-is('Office') + div.description",
        }

    def is_transient_error(self, error):
        return any(e in str(error) for e in [
            "Timeout", "NetworkError", "ConnectionReset"
        ])

    def is_structural_error(self, error):
        return any(e in str(error) for e in [
            "selector", "element", "not found", "Missing"
        ])

    async def retry_with_backoff(self, func, *args, **kwargs):
        for attempt in range(self.NETWORK_RETRIES):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                # Classify error
                if self.is_transient_error(e):
                    error_type = TransientError
                elif self.is_structural_error(e):
                    error_type = StructuralError
                else:
                    error_type = PermanentError

                # Handle based on type
                if not isinstance(e, TransientError) or attempt == self.NETWORK_RETRIES - 1:
                    raise error_type from e

                wait_time = self.NETWORK_RETRY_DELAY * (2 ** attempt)
                await asyncio.sleep(wait_time)

    # PHASE 1 METHODS   - start here
    async def write_batch_to_csv(self, records, filename):
        """Write records to CSV file"""
        file_exists = os.path.exists(filename)
        with open(filename, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["page_found", "notice_id", "url"])
            if not file_exists:
                writer.writeheader()
            writer.writerows(records)

    async def process_page(self, page, page_num):
        """Process a single page and extract notice links"""
        async def _process_page_inner():
            await page.wait_for_selector("#main-container > app-frontend-search-home > div > div > div > div.desktop\\:grid-col-8.tablet-lg\\:grid-col-12.mobile-lg\\:grid-col-12 > search-list-layout > div:nth-child(2) > div > div > sds-search-result-list > div:nth-child(1) > div > app-opportunity-result > div > div.grid-col-12.tablet\\:grid-col-9 > div:nth-child(1) > div > h3 > a", timeout=self.NETWORK_TIMEOUT)

            max_page_element = await page.query_selector("#main-container > app-frontend-search-home > div > div > div > div.desktop\\:grid-col-8.tablet-lg\\:grid-col-12.mobile-lg\\:grid-col-12 > search-list-layout > div:nth-child(2) > div > sds-pagination > div.sds-pagination.ng-star-inserted > div.sds-pagination__controls > span > strong")
            if max_page_element:
                self._max_pages = int(await max_page_element.inner_html())
                print(f"Max pages: {self._max_pages}")

            notice_links = await page.query_selector_all("#main-container > app-frontend-search-home > div > div > div > div.desktop\\:grid-col-8.tablet-lg\\:grid-col-12.mobile-lg\\:grid-col-12 > search-list-layout > div:nth-child(2) > div > div > sds-search-result-list > div > div > app-opportunity-result > div > div.grid-col-12.tablet\\:grid-col-9 > div:nth-child(1) > div > h3 > a")

            if not notice_links:
                return None, []

            page_records = []
            new_notice_ids = []
            existing_notice_ids = self.progress_tracker.get_extracted_url_ids()
            existing_ids = {record["url_id"] for record in existing_notice_ids}

            for link in notice_links:
                href = await link.get_attribute("href")
                notice_id = href.split("/")[-2]
                url_id = f"https://sam.gov{href}"
                print(notice_id)

                # print([u["url_id"] for u in existing_notice_ids])
                # Skip if already extracted
                if notice_id in existing_ids:
                    print(f"⏭️ Skipping already extracted notice: {notice_id}")
                    continue

                record = {
                    "page_found": page_num,
                    "url_id": notice_id,
                    "url": f"https://sam.gov/opp/{notice_id}/view"
                }
                page_records.append(record)
                new_notice_ids.append(record)
                # new_notice_ids.append(notice_id)

            return len(page_records), new_notice_ids, page_records

        try:
            return await self.retry_with_backoff(_process_page_inner)
        except Exception as e:
            print(f"❌ Failed to process page {page_num}: {str(e)}")
            return None, [], []

    async def scrape_url(self, url, state_name):
        """Scrape a single URL (state)"""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            total_records = 0
            none_count = 0

            try:
                page = await context.new_page()
                await self.retry_with_backoff(page.goto, url, timeout=self.NETWORK_TIMEOUT)

                # Process first page
                result = await self.process_page(page, 1)
                if result[0] is not None:
                    records_count, new_notice_ids, page_records = result
                    if page_records:
                        if self.fresh_re_run == False:
                            self.progress_tracker.add_extracted_url_ids(page_records)
                            total_records += records_count
                        else:
                            self.progress_tracker.add_newly_extracted_url_ids(page_records)
                            total_records += records_count

                        print(f"✅ Processed page 1: {records_count} new records")

                # Process remaining pages
                for page_num in range(2, self._max_pages + 1):
                    result = await self.process_page(page, page_num)

                    if result[0] is None:
                        none_count += 1
                        print(f"❌ No records found on page {page_num}. None count: {none_count}")
                        if none_count >= self.MAX_NONE_RESPONSES:
                            print(f"Reached maximum None responses for {state_name}")
                            break
                    else:
                        records_count, new_notice_ids, page_records = result
                        none_count = 0
                        if page_records:
                            if self.fresh_re_run == False:
                                self.progress_tracker.add_extracted_url_ids(page_records)
                                total_records += records_count
                            else:
                                self.progress_tracker.add_newly_extracted_url_ids(page_records)
                                total_records += records_count

                            # self.progress_tracker.add_extracted_url_ids(page_records)
                            # total_records += records_count
                            print(f"✅ Processed page {page_num}: {records_count} new records")

                    try:
                        await page.click("button#bottomPagination-nextPage", timeout=self.NETWORK_TIMEOUT)
                        await asyncio.sleep(self.REQUEST_DELAY)
                    except Exception:
                        print("Cannot find next page button. Moving to next page.")
                        break
            except PermanentError as pe:
                    self.progress_tracker.add_phase1_failure(state_name, pe)
            except StructuralError as se:
                print(f"⚠️ Structural error in {state_name}: {se}")
                self.progress_tracker.add_phase1_failure(state_name, se)
            except Exception as e:
                print(f"❌ Unhandled error in {state_name}: {e}")
                self.progress_tracker.add_phase1_failure(state_name, e)
            # except Exception as e:
            #     print(f"❌ Error processing {state_name}: {e}")
            #     raise e
            finally:
                await browser.close()

            return total_records

    async def run_phase1(self):
        """Run Phase 1 - Extract notice IDs and URLs"""
        print("🚀 Starting Phase 1: Notice ID and URL extraction")
        start_time = time()

        # Get starting point from progress
        last_completed_index = self.progress_tracker.get_last_completed_state_index()
        start_index = last_completed_index + 1

        if start_index >= len(self.URLS):
            print("✅ All states already completed in Phase 1")
            return 0

        print(f"📍 Resuming from state index {start_index}")

        grand_total = 0
        state_names = list(self.states.values())

        for index in range(start_index, len(self.URLS)):
            url = self.URLS[index]
            state_name = state_names[index]

            print(f"\n🔄 Processing {state_name} (Index: {index})")

            try:
                url_start = time()
                total_records = await self.scrape_url(url, state_name)
                url_time = time() - url_start

                # Mark state as completed
                self.progress_tracker.mark_state_completed(index, state_name)

                grand_total += total_records
                print(f"✅ Completed {state_name}: {total_records} new records in {url_time:.2f}s")

            except Exception as e:
                print(f"❌ Failed to process {state_name}: {str(e)}")
                print("💾 Progress saved. You can resume from here.")
                break

        total_time = time() - start_time
        print(f"\n📊 Phase 1 Complete - {grand_total} total new records in {total_time:.2f}s")
        self.progress_tracker.update_timestamp("phase1")
        return grand_total
# PHASE 1 METHODS ends here 


    # PHASE 2 METHODS starts heree
    async def extract_naics_code(self, text):
        """Extract NAICS code from text"""
        if not text:
            return None
        match = re.match(r'^(\d{6})', text)
        return match.group(1) if match else None

    async def extract_city_state_zip(self, text):
        """Extract city, state, and ZIP from address text"""
        if not text:
            return None
        match = re.match(r"([\w\s]+), ([A-Z]{2}) (\d{5}(-\d{4})?)", text)
        if match:
            return {
                "city": match.group(1).strip(),
                "state": match.group(2),
                "zip_code": match.group(3)
            }
        return None

    async def extract_data(self, page, notice):
        """Extract detailed data from a notice page"""
        async def _extract_data_inner():
            await page.goto(notice["url"], timeout=self.NETWORK_TIMEOUT)
            await page.wait_for_selector('#header-solicitation-number > div > div.header', timeout=self.NETWORK_TIMEOUT)

            async def get_text(selector):
                try:
                    element = await page.query_selector(selector)
                    if element:
                        return await element.inner_text()
                    return ""
                except Exception:
                    return ""

            data = {"Rfp_url": notice["url"]}
            data["Notice_Id"] = await get_text(self.selectors["notice_id"])
            data["Title"] = await get_text(self.selectors["title"])
            data["Description"] = await get_text(self.selectors["description"])

            naics_text = await get_text(self.selectors["naics"])
            data["Naics"] = await self.extract_naics_code(naics_text)

            data["Value"] = await get_text(self.selectors["value"])
            data["Due Date"] = await get_text(self.selectors["due_date"])
            data["End Date"] = await get_text(self.selectors["end_date"])
            data["Primary Contact Name"] = await get_text(self.selectors["contact_name"])
            data["Primary Contact Email"] = await get_text(self.selectors["contact_email"])
            data["Primary Contact Phone"] = await get_text(self.selectors["contact_phone"])

            data["Company"] = await get_text(self.selectors["department_ind_agency"])
            data["Industries"] = await get_text(self.selectors["sub_tier"])
            data["Office"] = await get_text(self.selectors["office"])
            data["Address"] = await get_text(self.selectors["address"])

            city_address = await get_text(self.selectors["city_address"])
            city_state = await self.extract_city_state_zip(city_address)

            data["City"] = city_state["city"] if city_state else ""
            data["State"] = city_state["state"] if city_state else ""
            data["Zipcode"] = city_state["zip_code"] if city_state else ""

            return data

        return await self.retry_with_backoff(_extract_data_inner)

    async def write_phase2_batch(self, batch, filename):
        """Write batch of phase 2 results to CSV"""
        if not batch:
            return

        file_exists = os.path.exists(filename)
        with open(filename, mode="a", newline='', encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=batch[0].keys())
            if not file_exists:
                writer.writeheader()
            writer.writerows(batch)
            print(f"💾 Wrote {len(batch)} records to {filename}")

    async def run_phase2(self, input_csv: Optional[str] = None):
        """Run Phase 2 - Extract detailed information"""
        print("🚀 Starting Phase 2: Detailed information extraction")

        # Filter out already processed notices
        existing_notice_ids = self.progress_tracker.get_extracted_url_ids()
        new_notice_ids = self.progress_tracker.get_newly_extracted_url_ids()
        
        processed_ids = self.progress_tracker.get_processed_notice_ids()
        # print([n for n in existing_notice_ids])
        if self.fresh_re_run == False:
            print(f"📂 Using extracted url_ids from phase in the progress file, total number is: {len(existing_notice_ids)}")
            unprocessed_notices = [
                notice for notice in existing_notice_ids
                if notice["url_id"] not in processed_ids
            ]
        else:
            print(f"📂 Using extracted url_ids from phase in the progress file, total number is: {len(new_notice_ids)}")
            unprocessed_notices = [
                notice for notice in new_notice_ids
                if notice["url_id"] not in processed_ids
            ]

        if not unprocessed_notices:
            print("✅ All notices already processed in Phase 2")
            return 0

        print(f"📊 Found {len(unprocessed_notices)} unprocessed notices")

        # Process notices
        semaphore = asyncio.Semaphore(self.MAX_CONCURRENT)
        batch_results = []
        processed_count = 0

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)

            async def process_notice(notice):
                nonlocal batch_results, processed_count
                async with semaphore:
                    context = await browser.new_context()
                    page = await context.new_page()
                    try:
                        print(f"🔎 Processing {notice['url_id']} ({processed_count + 1}/{len(unprocessed_notices)})")

                        await asyncio.sleep(random.uniform(1, 3))
                        data = await self.extract_data(page, notice)
                        await asyncio.sleep(random.uniform(1, 3))

                        if data:
                            batch_results.append(data)
                            self.progress_tracker.add_processed_notice_id(notice["url_id"])
                            processed_count += 1
                            print(f"✅ Done: {notice}")

                    except Exception as e:
                        print(f"❌ Failed: {notice['url_id']} - {str(e)}")
                    finally:
                        await context.close()

                    # Write batch if needed
                    if len(batch_results) >= self.PHASE2_BATCH_SIZE:
                        await self.write_phase2_batch(batch_results, self.PHASE2_OUTPUT_CSV)
                        batch_results = []

            # Process all notices
            await asyncio.gather(*[process_notice(notice) for notice in unprocessed_notices])

            # Write remaining batch
            if batch_results:
                await self.write_phase2_batch(batch_results, self.PHASE2_OUTPUT_CSV)

            await browser.close()

        print(f"📊 Phase 2 Complete - {processed_count} records processed")
        self.progress_tracker.progress_data["phase1"]["extracted_url_ids"].extend(self.progress_tracker.progress_data["phase1"]["newly_extracted_url_ids"])
        self.progress_tracker.progress_data["phase1"]["newly_extracted_url_ids"] = []
        self.progress_tracker.update_timestamp("phase2")
        return processed_count

    async def run_both_phases(self):
        """Run both phases sequentially"""
        print("🚀 Starting Combined Scraping Process")

        # Run Phase 1
        phase1_count = await self.run_phase1()

        # Run Phase 2 only if Phase 1 found new records or Phase 2 is incomplete
        processed_ids = self.progress_tracker.get_processed_notice_ids()
        extracted_ids = self.progress_tracker.get_extracted_url_ids()
        extracted_id_urls = [n_ids["url_id"] for n_ids in extracted_ids]
        unprocessed_count = len(set(extracted_id_urls) - processed_ids)

        if phase1_count > 0 or unprocessed_count > 0:
            print(f"\n🔄 Proceeding to Phase 2 ({unprocessed_count} unprocessed notices)")
            phase2_count = await self.run_phase2()
        else:
            print("\n⏭️ Skipping Phase 2 - no new or unprocessed notices found")
            phase2_count = 0

        print(f"\n🎉 Complete! Phase 1: {phase1_count} new notices, Phase 2: {phase2_count} processed")

def main():
    parser = argparse.ArgumentParser(description="Combined SAM.gov Scraper")
    parser.add_argument("--type", choices=["retry", "fresh"], default="retry", help="Run type: retry attempt or a fresh rerun")

    args = parser.parse_args()

    scraper = CombinedScraper()
    if args.type == 'fresh':
        scraper.fresh_re_run = True
        scraper.progress_tracker.progress_data["phase1"]["last_completed_state_index"] = -1
        scraper.progress_tracker.progress_data["phase1"]["completed_states"] = []
        scraper.progress_tracker.save_progress()
        scraper.progress_tracker.progress_data["phase1"]["newly_extracted_url_ids"] = [] #always make it blank

    asyncio.run(scraper.run_both_phases())

if __name__ == "__main__":
    main()
