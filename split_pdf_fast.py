import os
import glob
import math
import json
import shutil
import time
import unicodedata
from concurrent.futures import ProcessPoolExecutor, as_completed
from pypdf import PdfReader, PdfWriter

TARGET_SIZE_MB = 4.8  # Use slightly less than 5MB to be safe
TARGET_SIZE_BYTES = TARGET_SIZE_MB * 1024 * 1024
OUTPUT_DIR = "splitted_pdfs"
MANIFEST_FILE = "split_manifest.json"

def process_chunk(input_path, output_path, start_page, end_page):
    """
    Worker function to process a specific chunk of a PDF.
    Reading internally because PdfReader can't be easily pickled.
    """
    reader = PdfReader(input_path)
    writer = PdfWriter()
    
    # end_page is exclusive
    for i in range(start_page, end_page):
        writer.add_page(reader.pages[i])
        
    with open(output_path, "wb") as f_out:
        writer.write(f_out)
        
    return output_path

def process_pdf(pdf_path):
    """
    Analyzes a PDF, and if it exceeds the limit, calculates chunks and splits it.
    Returns (pdf_path, is_split, list_of_subfiles).
    """
    file_size = os.path.getsize(pdf_path)
    if file_size <= TARGET_SIZE_BYTES:
        return (pdf_path, False, [])

    print(f"Splitting {pdf_path} ({file_size / (1024*1024):.2f} MB)...")
    try:
        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)
    except Exception as e:
        print(f"Error reading {pdf_path}: {e}")
        return (pdf_path, False, [])

    # Estimate pages per chunk assuming linear distribution
    num_chunks = math.ceil(file_size / TARGET_SIZE_BYTES)
    pages_per_chunk = math.ceil(total_pages / num_chunks)
    
    # In case files are highly compressed or irregular, we ensure at least 1 page.
    if pages_per_chunk < 1:
        pages_per_chunk = 1

    base_name = os.path.basename(pdf_path)
    name, ext = os.path.splitext(base_name)
    
    tasks = []
    subfiles = []
    
    for i in range(num_chunks):
        start_page = i * pages_per_chunk
        end_page = min((i + 1) * pages_per_chunk, total_pages)
        
        if start_page >= total_pages:
            break
            
        out_name = f"{name}_part{i+1}{ext}"
        out_path = os.path.join(OUTPUT_DIR, out_name)
        
        subfiles.append(out_path)
        tasks.append((pdf_path, out_path, start_page, end_page))

    return (pdf_path, True, tasks, subfiles)

def main():
    start_time = time.time()
    
    # 1. Force cut preparation: Delete old outputs
    if os.path.exists(OUTPUT_DIR):
        print(f"[{time.strftime('%X')}] Deleting old {OUTPUT_DIR}...")
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR)
    
    # Find all PDFs in the current directory and subdirectories (excluding splitted_pdfs)
    all_pdfs = glob.glob("**/*.pdf", recursive=True)
    target_pdfs = [p for p in all_pdfs if not p.startswith(OUTPUT_DIR)]
    
    manifest = {
        "_comment": "This file is auto-generated. It maps original large PDF files to their split chunks for lazy loading.",
        "large_files_to_ignore": [],
        "groups": {}
    }
    
    print(f"[{time.strftime('%X')}] Found {len(target_pdfs)} PDFs to inspect.")
    
    # We will use multiprocessing twice.
    # First, to fast-inspect PDFs & prepare split tasks (PdfReader can take some time).
    all_chunk_tasks = []
    
    with ProcessPoolExecutor() as executor:
        # Submit all analysis jobs
        futures = {executor.submit(process_pdf, pdf): pdf for pdf in target_pdfs}
        for future in as_completed(futures):
            pdf_path, is_split, result_tasks_or_none, subfiles = future.result() if len(future.result()) == 4 else (*future.result(), None)
            
            if is_split:
                manifest["large_files_to_ignore"].append(pdf_path)
                manifest["groups"][pdf_path] = subfiles
                all_chunk_tasks.extend(result_tasks_or_none)
    
    if all_chunk_tasks:
        print(f"[{time.strftime('%X')}] Found {len(all_chunk_tasks)} chunks to generate. Processing in parallel...")
        # Second multiprocessing pool for actually doing the extraction
        with ProcessPoolExecutor() as executor:
            chunk_futures = [executor.submit(process_chunk, *task) for task in all_chunk_tasks]
            
            completed = 0
            for future in as_completed(chunk_futures):
                try:
                    generated_file = future.result()
                    completed += 1
                    if completed % 10 == 0 or completed == len(all_chunk_tasks):
                        print(f"[{time.strftime('%X')}] Progress: {completed}/{len(all_chunk_tasks)} chunks created.")
                except Exception as exc:
                    print(f"Chunk processing generated an exception: {exc}")
    else:
        print(f"[{time.strftime('%X')}] No large PDFs found that needed splitting.")

    # Auto update .gitignore to ignore large origin files
    # macOS uses NFD (decomposed) unicode for filenames, so we must normalize
    # all paths to NFD before writing to .gitignore for git to match them.
    gitignore_path = ".gitignore"
    MARKER_START = "# >>> AUTO-ADDED BY SPLIT PDF SCRIPT (DO NOT EDIT) >>>"
    MARKER_END = "# <<< END SPLIT PDF SCRIPT <<<"
    
    # Read existing .gitignore, strip out previous auto-generated block
    existing_lines = []
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r", encoding="utf-8") as f:
            in_auto_block = False
            for line in f:
                stripped = line.rstrip("\n")
                if stripped == MARKER_START:
                    in_auto_block = True
                    continue
                if stripped == MARKER_END:
                    in_auto_block = False
                    continue
                if not in_auto_block:
                    existing_lines.append(line)
    
    # Normalize paths to NFC for .gitignore (git uses NFC with core.precomposeunicode=true)
    ignore_paths = [unicodedata.normalize("NFC", p) for p in manifest["large_files_to_ignore"]]
    
    # Rewrite .gitignore with the auto block at the end
    with open(gitignore_path, "w", encoding="utf-8") as f:
        for line in existing_lines:
            f.write(line)
        if ignore_paths:
            f.write("\n" + MARKER_START + "\n")
            for p in sorted(ignore_paths):
                f.write(p + "\n")
            f.write(MARKER_END + "\n")
    
    print(f"[{time.strftime('%X')}] Updated .gitignore with {len(ignore_paths)} large origin files.")

    # Remove tracked origin files from git cache (gitignore only prevents NEW tracking)
    import subprocess
    # Use -z for null-separated raw UTF-8 output (git quotes unicode filenames otherwise)
    tracked_result = subprocess.run(
        ["git", "ls-files", "--cached", "-z"],
        capture_output=True, cwd="."
    )
    # Split by null byte, decode as UTF-8
    raw_tracked = [f for f in tracked_result.stdout.decode("utf-8").split("\0") if f]
    
    # Build NFC→original mapping for tracked files (git returns NFC due to core.precomposeunicode)
    tracked_nfc_map = {unicodedata.normalize("NFC", f): f for f in raw_tracked}
    
    # Match ignore_paths (which may be NFD from glob) against tracked files (NFC from git)
    files_to_untrack = []
    for p in ignore_paths:
        nfc_p = unicodedata.normalize("NFC", p)
        if nfc_p in tracked_nfc_map:
            # Use the original git path for git rm
            files_to_untrack.append(tracked_nfc_map[nfc_p])
    
    if files_to_untrack:
        print(f"[{time.strftime('%X')}] Removing {len(files_to_untrack)} origin files from git tracking...")
        result = subprocess.run(
            ["git", "rm", "--cached", "--quiet"] + files_to_untrack,
            capture_output=True, text=True, cwd="."
        )
        if result.returncode == 0:
            print(f"[{time.strftime('%X')}] Done! {len(files_to_untrack)} files untracked (still exist on disk).")
        else:
            print(f"[{time.strftime('%X')}] Warning: {result.stderr.strip()}")

    # Write manifest
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        
    # Write Readme
    readme_content = f"""# PDF Splitting Result

This folder (`{OUTPUT_DIR}`) contains PDF files that were auto-split because their origins exceeded ~5MB.

Since GitHub doesn't support lazy-loading partially from huge monolithic files smoothly, these split files can be used.
Other projects reading the PDFs should refer to `{MANIFEST_FILE}` which defines:
1. `large_files_to_ignore`: A list of original root PDFs. Your game/app should ignore these completely to save bandwidth.
2. `groups`: A mapping where the key is the ignored original PDF, and the value is an array of sub-files (chunks). The chunks must be loaded and rendered as if they were a single document.

Any PDF not listed in `large_files_to_ignore` can be loaded normally.

### How to use via mapping:
```javascript
import manifest from './{MANIFEST_FILE}';

function getPdfPathsToLoad(originalPath) {{
    if (manifest.large_files_to_ignore.includes(originalPath)) {{
        return manifest.groups[originalPath]; // array of split parts
    }}
    return [originalPath]; // directly return original small file
}}
```
"""
    with open("README_PDF_SPLITTER.md", "w", encoding="utf-8") as f:
        f.write(readme_content)
        
    elapsed = time.time() - start_time
    print(f"[{time.strftime('%X')}] Done in {elapsed:.2f} seconds!")
    print(f"[{time.strftime('%X')}] Generated {MANIFEST_FILE} and README_PDF_SPLITTER.md")

if __name__ == "__main__":
    main()
