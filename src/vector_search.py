"""
Vector Search Module for AI SRE Agent
=====================================
Uses Qdrant for storing and retrieving similar past incidents.
"""

import os
from typing import List, Dict, Optional
from datetime import datetime

# Qdrant client
try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams, PointStruct
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False
    print("⚠️ Qdrant client not installed")

# For embeddings, we'll use Groq's embedding or a simple hash-based approach
import hashlib

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
COLLECTION_NAME = "sre_incidents"
VECTOR_SIZE = 48  # SHA384 hash produces 48 bytes (fallback), or use 384 for sentence-transformers

class VectorSearch:
    def __init__(self):
        self.client = None
        self.embedding_model = None
        self._init_client()
        self._init_embedding()
    
    def _init_client(self):
        """Initialize Qdrant client."""
        if not QDRANT_AVAILABLE:
            return
        
        try:
            self.client = QdrantClient(url=QDRANT_URL)
            # Create collection if not exists
            collections = self.client.get_collections().collections
            if not any(c.name == COLLECTION_NAME for c in collections):
                self.client.create_collection(
                    collection_name=COLLECTION_NAME,
                    vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
                )
                print(f"✅ Created Qdrant collection: {COLLECTION_NAME}")
            else:
                print(f"✅ Connected to Qdrant collection: {COLLECTION_NAME}")
        except Exception as e:
            print(f"⚠️ Qdrant connection failed: {e}")
            self.client = None
    
    def _init_embedding(self):
        """Initialize embedding model."""
        try:
            from sentence_transformers import SentenceTransformer
            self.embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
            print("✅ Sentence transformer model loaded")
        except ImportError:
            print("⚠️ sentence-transformers not installed, using fallback")
            self.embedding_model = None
        except Exception as e:
            print(f"⚠️ Embedding model failed: {e}")
            self.embedding_model = None
    
    def embed(self, text: str) -> List[float]:
        """Create embedding for text."""
        if self.embedding_model:
            return self.embedding_model.encode(text).tolist()
        else:
            # Fallback: simple hash-based pseudo-embedding
            hash_bytes = hashlib.sha384(text.encode()).digest()
            return [float(b) / 255.0 for b in hash_bytes]
    
    def store_incident(self, incident: Dict) -> bool:
        """Store an incident in the vector database."""
        if not self.client:
            return False
        
        try:
            # Create text for embedding
            text = f"""
            Alert: {incident.get('alert_name', '')}
            Severity: {incident.get('severity', '')}
            Description: {incident.get('description', '')}
            Analysis: {incident.get('ai_analysis', '')}
            Action: {incident.get('action_taken', '')}
            """
            
            vector = self.embed(text)
            
            # Use incident ID as point ID (must be positive integer or UUID)
            point_id = incident.get('id', int(datetime.now().timestamp() * 1000))
            
            self.client.upsert(
                collection_name=COLLECTION_NAME,
                points=[
                    PointStruct(
                        id=point_id,
                        vector=vector,
                        payload={
                            "alert_name": incident.get('alert_name', ''),
                            "severity": incident.get('severity', ''),
                            "description": incident.get('description', ''),
                            "action_taken": incident.get('action_taken', ''),
                            "ai_analysis": incident.get('ai_analysis', ''),
                            "timestamp": incident.get('timestamp', datetime.now().isoformat()),
                            "verified": incident.get('verified', False)
                        }
                    )
                ]
            )
            print(f"📦 Stored incident {point_id} in vector DB")
            return True
        except Exception as e:
            print(f"❌ Failed to store incident: {e}")
            return False
    
    def search_similar(self, alert: Dict, limit: int = 3) -> List[Dict]:
        """Search for similar past incidents."""
        if not self.client:
            return []
        
        try:
            # Create query text
            text = f"""
            Alert: {alert.get('labels', {}).get('alertname', '')}
            Severity: {alert.get('labels', {}).get('severity', '')}
            Description: {alert.get('annotations', {}).get('description', '')}
            """
            
            query_vector = self.embed(text)
            
            results = self.client.query_points(
                collection_name=COLLECTION_NAME,
                query=query_vector,
                limit=limit,
                score_threshold=0.3  # Lower threshold for hash-based similarity
            ).points
            
            similar_incidents = []
            for result in results:
                similar_incidents.append({
                    "score": result.score,
                    "alert_name": result.payload.get('alert_name', ''),
                    "action_taken": result.payload.get('action_taken', ''),
                    "ai_analysis": result.payload.get('ai_analysis', ''),
                    "verified": result.payload.get('verified', False)
                })
            
            if similar_incidents:
                print(f"🔍 Found {len(similar_incidents)} similar incidents")
            
            return similar_incidents
        except Exception as e:
            print(f"❌ Search failed: {e}")
            return []
    
    def get_context_prompt(self, alert: Dict) -> str:
        """Get context from similar incidents for the AI prompt."""
        similar = self.search_similar(alert)
        
        if not similar:
            return ""
        
        context = "\n\nPAST SIMILAR INCIDENTS:\n"
        for i, incident in enumerate(similar, 1):
            context += f"""
{i}. {incident['alert_name']} (similarity: {incident['score']:.2f})
   - Action taken: {incident['action_taken']}
   - Verified: {'Yes' if incident['verified'] else 'No'}
"""
        
        context += "\nConsider these past incidents when deciding on the best action.\n"
        return context


# Global instance
vector_search = None

def get_vector_search() -> Optional[VectorSearch]:
    """Get or create VectorSearch instance."""
    global vector_search
    if vector_search is None:
        vector_search = VectorSearch()
    return vector_search
