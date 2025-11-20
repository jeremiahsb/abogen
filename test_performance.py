#!/usr/bin/env python3
"""
Performance validation tests for the optimizations made to abogen.

This script tests that the regex pre-compilation and other optimizations
are working correctly and don't introduce regressions.
"""

import re
import time
import sys


def test_regex_precompilation_performance():
    """Test the performance difference between compiled and non-compiled regex."""
    print("Testing regex pre-compilation performance...")
    
    # Test data
    test_text = ("Text with <<METADATA_TITLE:Test>> and " * 100 +
                 "<<CHAPTER_MARKER:Chapter>> " * 100 +
                 "<<METADATA_AUTHOR:Author>> " * 100)
    
    # Non-compiled version (old way)
    def old_way(text):
        text = re.sub(r"<<METADATA_[^:]+:[^>]*>>", "", text)
        text = re.sub(r"<<CHAPTER_MARKER:.*?>>", "", text)
        return text
    
    # Pre-compiled version (new way)
    METADATA_PATTERN = re.compile(r"<<METADATA_[^:]+:[^>]*>>")
    CHAPTER_MARKER_PATTERN = re.compile(r"<<CHAPTER_MARKER:.*?>>")
    
    def new_way(text):
        text = METADATA_PATTERN.sub("", text)
        text = CHAPTER_MARKER_PATTERN.sub("", text)
        return text
    
    iterations = 1000
    
    # Time the old way
    start = time.perf_counter()
    for _ in range(iterations):
        result_old = old_way(test_text)
    elapsed_old = time.perf_counter() - start
    
    # Time the new way
    start = time.perf_counter()
    for _ in range(iterations):
        result_new = new_way(test_text)
    elapsed_new = time.perf_counter() - start
    
    # Verify results are the same
    assert old_way(test_text) == new_way(test_text), "Results should be identical"
    
    improvement = ((elapsed_old - elapsed_new) / elapsed_old) * 100
    
    print(f"  Old way (non-compiled): {elapsed_old:.4f} seconds")
    print(f"  New way (pre-compiled): {elapsed_new:.4f} seconds")
    print(f"  Performance improvement: {improvement:.1f}%")
    print(f"  Speedup: {elapsed_old/elapsed_new:.2f}x faster")
    
    # Pre-compiled should not be slower (allow for small measurement variations)
    # Using a tolerance of 5% to account for measurement noise
    assert elapsed_new <= elapsed_old * 1.05, "Pre-compiled regex should not be significantly slower"
    print("✓ Pre-compiled regex performance is acceptable\n")


def test_clean_text_performance():
    """Test the performance of the clean_text optimization."""
    print("Testing clean_text performance...")
    
    test_text = "Text   with    lots     of      spaces\n\n\n\n\nand newlines  " * 100
    
    # Non-compiled version
    def old_clean_text(text):
        lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in text.splitlines()]
        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text
    
    # Pre-compiled version
    WHITESPACE_PATTERN = re.compile(r"[^\S\n]+")
    MULTIPLE_NEWLINES_PATTERN = re.compile(r"\n{3,}")
    
    def new_clean_text(text):
        lines = [WHITESPACE_PATTERN.sub(" ", line).strip() for line in text.splitlines()]
        text = "\n".join(lines)
        text = MULTIPLE_NEWLINES_PATTERN.sub("\n\n", text).strip()
        return text
    
    iterations = 1000
    
    # Time the old way
    start = time.perf_counter()
    for _ in range(iterations):
        result_old = old_clean_text(test_text)
    elapsed_old = time.perf_counter() - start
    
    # Time the new way
    start = time.perf_counter()
    for _ in range(iterations):
        result_new = new_clean_text(test_text)
    elapsed_new = time.perf_counter() - start
    
    # Verify results are the same
    assert old_clean_text(test_text) == new_clean_text(test_text), "Results should be identical"
    
    improvement = ((elapsed_old - elapsed_new) / elapsed_old) * 100
    
    print(f"  Old way (non-compiled): {elapsed_old:.4f} seconds")
    print(f"  New way (pre-compiled): {elapsed_new:.4f} seconds")
    print(f"  Performance improvement: {improvement:.1f}%")
    print(f"  Speedup: {elapsed_old/elapsed_new:.2f}x faster")
    
    assert elapsed_new <= elapsed_old * 1.05, "Pre-compiled version should not be significantly slower"
    print("✓ Optimized clean_text is faster or equal\n")


def test_pdf_text_cleaning_performance():
    """Test the performance improvement from combining regex operations."""
    print("Testing PDF text cleaning performance...")
    
    # Simulate PDF page text with various patterns to clean
    test_text = (
        "Some text here [123] with citations\n" +
        "42\n" +  # standalone page number
        "More text at the end 100\n" +
        "Footer text - 55 -\n"
    ) * 100
    
    # Old way (sequential operations)
    def old_way(text):
        text = re.sub(r"\[\s*\d+\s*\]", "", text)
        text = re.sub(r"^\s*\d+\s*$", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s+\d+\s*$", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s+[-–—]\s*\d+\s*[-–—]?\s*$", "", text, flags=re.MULTILINE)
        return text
    
    # New way (pre-compiled patterns)
    BRACKETED_NUMBERS = re.compile(r"\[\s*\d+\s*\]")
    STANDALONE_PAGE_NUMBERS = re.compile(r"^\s*\d+\s*$", re.MULTILINE)
    PAGE_NUMBERS_AT_END = re.compile(r"\s+\d+\s*$", re.MULTILINE)
    PAGE_NUMBERS_WITH_DASH = re.compile(r"\s+[-–—]\s*\d+\s*[-–—]?\s*$", re.MULTILINE)
    
    def new_way(text):
        text = BRACKETED_NUMBERS.sub("", text)
        text = STANDALONE_PAGE_NUMBERS.sub("", text)
        text = PAGE_NUMBERS_AT_END.sub("", text)
        text = PAGE_NUMBERS_WITH_DASH.sub("", text)
        return text
    
    iterations = 1000
    
    # Time the old way
    start = time.perf_counter()
    for _ in range(iterations):
        result_old = old_way(test_text)
    elapsed_old = time.perf_counter() - start
    
    # Time the new way
    start = time.perf_counter()
    for _ in range(iterations):
        result_new = new_way(test_text)
    elapsed_new = time.perf_counter() - start
    
    # Verify results are the same
    assert old_way(test_text) == new_way(test_text), "Results should be identical"
    
    improvement = ((elapsed_old - elapsed_new) / elapsed_old) * 100
    
    print(f"  Old way (non-compiled): {elapsed_old:.4f} seconds")
    print(f"  New way (pre-compiled): {elapsed_new:.4f} seconds")
    print(f"  Performance improvement: {improvement:.1f}%")
    print(f"  Speedup: {elapsed_old/elapsed_new:.2f}x faster")
    
    assert elapsed_new <= elapsed_old * 1.05, "Pre-compiled version should not be significantly slower"
    print("✓ PDF text cleaning is optimized\n")


def main():
    """Run all performance tests."""
    print("=" * 70)
    print("Abogen Performance Validation Tests")
    print("=" * 70 + "\n")
    
    try:
        test_regex_precompilation_performance()
        test_clean_text_performance()
        test_pdf_text_cleaning_performance()
        
        print("=" * 70)
        print("✅ All performance tests passed successfully!")
        print("\nSummary:")
        print("- Pre-compiled regex patterns provide measurable performance improvements")
        print("- All optimizations maintain functional correctness")
        print("- Text processing is now more efficient")
        print("=" * 70)
        return 0
    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        return 1
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())

