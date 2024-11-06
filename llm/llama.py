# llama.py
import os
import csv
import time
import logging
import asyncio
import aiohttp
from datetime import datetime
from typing import List, Tuple, Dict

# =================== Hyperparameters ====================

# Paths to input files
FILTERED_CSV_PATH = 'dataset/filter.csv'
SIMILARITY_CSV_PATH = 'dataset/similarity_results.csv'
FILE_ENCODING = 'ISO-8859-1'

# Output directory
OUTPUT_DIR = 'dataset'

# Ollama API settings
OLLAMA_BASE_URL = 'http://localhost:11434'
MODEL_NAME = 'gemma2'

# Similarity threshold for comparing documents
SIMILARITY_THRESHOLD = 0.004  # Adjust as needed

# Async settings
BATCH_SIZE = 10  # Number of tasks to run concurrently

# Retry settings
MAX_RETRIES = 3

# Logging settings
LOGGING_LEVEL = logging.INFO
LOG_FILE = 'processing.log'

# ========================================================


class OllamaClient:
    def __init__(self):
        self.base_url = OLLAMA_BASE_URL

        # Set up logging
        logging.basicConfig(
            level=LOGGING_LEVEL,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(LOG_FILE),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)

        # Load prompts
        with open('llm/prompt/extract.txt', 'r', encoding=FILE_ENCODING) as f:
            self.extract_prompt = f.read()
        with open('llm/prompt/compare.txt', 'r', encoding=FILE_ENCODING) as f:
            self.compare_prompt = f.read()

    async def generate(self, session, prompt, stream=False, max_retries=MAX_RETRIES):
        """Asynchronous generate function with retry mechanism."""
        for attempt in range(max_retries):
            try:
                url = f"{self.base_url}/api/generate"
                payload = {
                    "model": MODEL_NAME,
                    "prompt": prompt,
                    "stream": stream
                }
                async with session.post(url, json=payload, timeout=120) as response:
                    response.raise_for_status()
                    result = await response.json()
                    return result
            except Exception as e:
                self.logger.warning(f"Attempt {attempt + 1} failed: {str(e)}")
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(2 ** attempt)  # Exponential backoff

    async def extract_claims(self, session, doc_id: str, text: str) -> List[Tuple[str, str, str]]:
        """Extract claims from text asynchronously."""
        try:
            prompt = self.extract_prompt.format(text=text)
            response = await self.generate(session, prompt)
            claims = []
            for line in response['response'].split('\n'):
                if line.strip() and line.lstrip().startswith(tuple('0123456789')):
                    parts = line.split('.', 1)
                    if len(parts) == 2:
                        claim = parts[1].strip()
                        if len(claim) > 10:  # Basic validation
                            claim_id = f"{doc_id.zfill(4)}{str(len(claims)+1).zfill(4)}"
                            claims.append((claim_id, claim, doc_id))
            if not claims:
                self.logger.warning(f"No valid claims extracted from document {doc_id}")
            return claims
        except Exception as e:
            self.logger.error(f"Failed to extract claims from document {doc_id}: {str(e)}")
            return []

    async def compare_claims(self, session, claim1_data: Tuple[str, str, str],
                             claim2_data: Tuple[str, str, str]) -> Tuple[str, str, int, str]:
        claim1_id, claim1_text, _ = claim1_data
        claim2_id, claim2_text, _ = claim2_data
        try:
            prompt = self.compare_prompt.format(claim1=claim1_text, claim2=claim2_text)
            response = await self.generate(session, prompt)
            result = response['response']

            output_lines = [line.strip() for line in result.split('\n') if line.strip().startswith('Output:')]
            if not output_lines:
                raise ValueError(f"No Output section found in response:\n{result}")

            output_line = output_lines[0]
            result_number = output_line.replace('Output:', '').strip()
            result_number = ''.join(filter(lambda x: x in '-0123456789', result_number))

            if result_number in ['1', '-1', '0']:
                return claim1_id, claim2_id, int(result_number), result
            else:
                raise ValueError(f"Invalid comparison result: {result_number} in response:\n{result}")
        except Exception as e:
            self.logger.error(f"Failed to compare claims {claim1_id} and {claim2_id}: {str(e)}")
            return claim1_id, claim2_id, 0, str(e)


async def process_documents():
    client = OllamaClient()
    claims_data = []
    relations_data = []

    # Create timestamp for this run
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Define output files
    claims_file = os.path.join(OUTPUT_DIR, f'claims_{timestamp}.csv')
    relations_file = os.path.join(OUTPUT_DIR, f'relations_{timestamp}.csv')

    # Ensure output directory exists
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Create empty relations file immediately
    with open(relations_file, 'w', newline='', encoding=FILE_ENCODING) as f:
        writer = csv.writer(f)
        writer.writerow(['id1', 'id2', 'relation', 'response'])

    # Check for existing claims file
    existing_claims_files = [f for f in os.listdir(OUTPUT_DIR) if f.startswith('claims_') and f.endswith('.csv')]

    if existing_claims_files:
        # Use the most recent claims file
        latest_claims_file = max(existing_claims_files)
        client.logger.info(f"Found existing claims file: {latest_claims_file}")

        with open(os.path.join(OUTPUT_DIR, latest_claims_file), 'r', encoding=FILE_ENCODING) as f:
            reader = csv.reader(f)
            next(reader)  # Skip header
            claims_data = [tuple(row) for row in reader]

        client.logger.info(f"Loaded {len(claims_data)} existing claims")
    else:
        # Load documents from filtered.csv
        client.logger.info(f"Loading documents from {FILTERED_CSV_PATH}")
        documents = []
        with open(FILTERED_CSV_PATH, 'r', encoding=FILE_ENCODING) as f:
            reader = csv.DictReader(f)
            for row in reader:
                documents.append({
                    'id': row['id'],
                    'title': row['title'],
                    'text': row['text'],
                    'validity': row['validity']
                })

        # Extract claims asynchronously
        client.logger.info(f"Extracting claims from {len(documents)} documents")

        async with aiohttp.ClientSession() as session:
            tasks = []
            for doc in documents:
                doc_id = doc['id']
                text = doc['text']
                task = asyncio.create_task(
                    client.extract_claims(session, doc_id, text)
                )
                tasks.append(task)

            # Gather all claim extraction tasks
            results = await asyncio.gather(*tasks)
            for claims in results:
                claims_data.extend(claims)

            # Save extracted claims
            with open(claims_file, 'w', newline='', encoding=FILE_ENCODING) as f:
                writer = csv.writer(f)
                writer.writerow(['claim_id', 'claim', 'document_id'])
                writer.writerows(claims_data)
            client.logger.info(f"Claim extraction complete. Total claims extracted: {len(claims_data)}")

    # Load similarity data
    client.logger.info(f"Loading similarity data from {SIMILARITY_CSV_PATH}")
    similarity_pairs = []
    with open(SIMILARITY_CSV_PATH, 'r', encoding=FILE_ENCODING) as f:
        reader = csv.DictReader(f)
        for row in reader:
            similarity = float(row['similarity'])
            if similarity >= SIMILARITY_THRESHOLD:
                similarity_pairs.append((row['id1'], row['id2']))

    client.logger.info(f"Total similar document pairs above threshold {SIMILARITY_THRESHOLD}: {len(similarity_pairs)}")

    # Build a mapping from document IDs to claims
    doc_claims_map: Dict[str, List[Tuple[str, str, str]]] = {}
    for claim in claims_data:
        claim_id, claim_text, doc_id = claim
        doc_claims_map.setdefault(doc_id, []).append(claim)

    # Prepare claim pairs for comparison based on similarity pairs
    comparison_tasks = []
    processed_pairs = set()

    async with aiohttp.ClientSession() as session:
        for idx, (doc_id1, doc_id2) in enumerate(similarity_pairs):
            claims1 = doc_claims_map.get(doc_id1, [])
            claims2 = doc_claims_map.get(doc_id2, [])

            for claim1_data in claims1:
                for claim2_data in claims2:
                    claim_pair_key = (claim1_data[0], claim2_data[0])
                    if claim_pair_key in processed_pairs:
                        continue
                    processed_pairs.add(claim_pair_key)

                    task = asyncio.create_task(
                        client.compare_claims(session, claim1_data, claim2_data)
                    )
                    comparison_tasks.append(task)

                    # If we've reached the batch size, process the batch
                    if len(comparison_tasks) >= BATCH_SIZE:
                        results = await asyncio.gather(*comparison_tasks)
                        for res in results:
                            # if res[2] != 0:  # Only store non-zero relations
                            relations_data.append(res)
                        # Save intermediate relations
                        with open(relations_file, 'a', newline='', encoding=FILE_ENCODING) as f:
                            writer = csv.writer(f)
                            writer.writerows(relations_data)
                        relations_data.clear()
                        comparison_tasks = []  # Reset tasks

            if idx % 10 == 0:
                client.logger.info(f"Processed {idx}/{len(similarity_pairs)} similar document pairs")

        # Process any remaining tasks
        if comparison_tasks:
            results = await asyncio.gather(*comparison_tasks)
            for res in results:
                # if res[2] != 0:
                relations_data.append(res)
            # Save final relations
            with open(relations_file, 'a', newline='', encoding=FILE_ENCODING) as f:
                writer = csv.writer(f)
                writer.writerows(relations_data)

    client.logger.info(f"Comparison complete. Total relations found: {len(relations_data)}")
    client.logger.info(f"Processing complete. Final results saved to {relations_file}")


def main():
    asyncio.run(process_documents())


if __name__ == "__main__":
    main()
