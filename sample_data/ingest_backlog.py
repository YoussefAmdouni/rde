"""
ingest_backlog.py — One-run script
===================================
Embeds every story in backlog.json and upserts into an existing Pinecone index.
The index must already exist in Pinecone before running this.

Embedding model : gemini-embedding-001 with output_dimensionality=768
Pinecone index  : must be created at 768 dimensions

Usage:
    python ingest_backlog.py --backlog sample_data/backlog.json --index backlog
"""

import os
import json
import argparse

from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_core.documents import Document
from langchain_pinecone import PineconeVectorStore

from dotenv import load_dotenv
load_dotenv(".env")

PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "backlog")
EMBEDDING_MODEL     = "models/gemini-embedding-001"
EMBEDDING_DIMS      = 768   # truncate output to fit Pinecone free tier (max 2048)


def build_documents(backlog: list[dict]) -> tuple[list[Document], list[str]]:
    """
    Text embedded = title + story + all acceptance criteria.
    Metadata carries the full story JSON so it can be retrieved
    alongside the similarity score with no extra lookup.
    """
    docs = []
    ids  = []

    for item in backlog:
        ac_text    = " ".join(item.get("acceptanceCriteria", []))
        embed_text = f"{item['title']}. {item['story']} {ac_text}".strip()

        doc = Document(
            page_content = embed_text,
            metadata     = {
                "story_id":   item["id"],
                "title":      item["title"],
                "priority":   item["priority"],
                "category":   item["category"],
                "story_json": json.dumps(item),
            }
        )
        docs.append(doc)
        ids.append(item["id"])   # story ID as vector ID → idempotent upsert

    return docs, ids


def main():
    parser = argparse.ArgumentParser(description="Embed and push backlog to Pinecone")
    parser.add_argument("--backlog", required=True, help="Path to backlog .json file")
    parser.add_argument("--index",   default=None,  help="Pinecone index name (default: from .env)")
    args = parser.parse_args()

    index_name = args.index or PINECONE_INDEX_NAME

    # Load backlog
    print(f"Loading backlog from: {args.backlog}")
    with open(args.backlog, "r", encoding="utf-8") as f:
        backlog = json.load(f)
    print(f"  {len(backlog)} stories loaded.")

    # Build documents
    docs, ids = build_documents(backlog)

    # Init embedding model — output_dimensionality truncates to 768 for free Pinecone
    print(f"Embedding model : {EMBEDDING_MODEL} (output_dimensionality={EMBEDDING_DIMS})")
    print(f"Pinecone index  : {index_name}")
    embeddings = GoogleGenerativeAIEmbeddings(
        model                  = EMBEDDING_MODEL,
        task_type              = "RETRIEVAL_DOCUMENT",
        output_dimensionality  = EMBEDDING_DIMS,
    )
    vector_store = PineconeVectorStore(
        index_name       = index_name,
        embedding        = embeddings,
        pinecone_api_key = os.environ["PINECONE_API_KEY"],
    )

    # Upsert all at once
    print(f"\nPushing {len(docs)} stories to Pinecone...")
    vector_store.add_documents(docs, ids=ids)

    print(f"\n  Done. {len(docs)} stories pushed to '{index_name}'.")


if __name__ == "__main__":
    main()