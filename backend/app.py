from flask import Flask, request, jsonify
import psycopg2
import os
import pika
import time
from flask_cors import CORS
import json
import redis
from functools import wraps

app = Flask(__name__)
CORS(app)

# Initialize Redis connection
redis_client = redis.Redis(
    host=os.getenv('REDIS_HOST', 'redis'),
    port=os.getenv('REDIS_PORT', 6379),
    decode_responses=True
)

# RabbitMQ connection setup
def get_rabbitmq_connection():
    while True:
        try:
            credentials = pika.PlainCredentials(
                os.getenv('RABBITMQ_DEFAULT_USER', 'guest'),
                os.getenv('RABBITMQ_DEFAULT_PASS', 'guest')
            )
            parameters = pika.ConnectionParameters(
                host=os.getenv('RABBITMQ_HOST', 'rabbitmq'),
                port=5672,
                virtual_host='/',
                credentials=credentials,
                heartbeat=600,
                blocked_connection_timeout=300
            )
            return pika.BlockingConnection(parameters)
        except pika.exceptions.AMQPConnectionError:
            time.sleep(5)

# Initialize RabbitMQ connection and channel
rabbitmq_conn = get_rabbitmq_connection()
rabbitmq_channel = rabbitmq_conn.channel()
rabbitmq_channel.queue_declare(queue='book_events')

# Database connection setup
def get_db_connection():
    return psycopg2.connect(
        dbname=os.environ['POSTGRES_DB'],
        user=os.environ['POSTGRES_USER'],
        password=os.environ['POSTGRES_PASSWORD'],
        host='db'
    )

def publish_book_event(event_type, book_data):
    try:
        message = {
            'event': event_type,
            'data': book_data,
            'timestamp': time.time()
        }
        rabbitmq_channel.basic_publish(
            exchange='',
            routing_key='book_events',
            body=json.dumps(message)
        )
    except Exception as e:
        print(f"Failed to publish message: {e}")

# Cache decorator
def cache_response(timeout=60):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            cache_key = f"books:{request.path}:{str(kwargs)}"
            cached = redis_client.get(cache_key)
            if cached:
                return jsonify(json.loads(cached))
            
            response = f(*args, **kwargs)
            redis_client.setex(cache_key, timeout, response.get_data(as_text=True))
            return response
        return wrapper
    return decorator

# Invalidate cache helper
def invalidate_books_cache():
    keys = redis_client.keys("books:*")
    if keys:
        redis_client.delete(*keys)

@app.route('/health')
def health_check():
    try:
        # Test database connection
        conn = get_db_connection()
        conn.close()
        # Test Redis connection
        redis_client.ping()
        # Test RabbitMQ connection
        rabbitmq_conn.channel()
        return jsonify({"status": "healthy"}), 200
    except Exception as e:
        return jsonify({"status": "unhealthy", "error": str(e)}), 500

@app.route('/books', methods=['GET'])
@cache_response(timeout=30)  # Cache for 30 seconds
def get_books():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM books;')
    books = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(books)

@app.route('/books', methods=['POST'])
def add_book():
    data = request.json
    name = data['name']
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('INSERT INTO books (name) VALUES (%s) RETURNING id, name;', (name,))
    book = cur.fetchone()
    conn.commit()
    
    # Invalidate cache and publish event
    invalidate_books_cache()
    publish_book_event('book_created', {'id': book[0], 'name': book[1]})
    
    cur.close()
    conn.close()
    return jsonify({'message': 'Book added', 'book': book})

@app.route('/books/<int:id>', methods=['PUT'])
def update_book(id):
    data = request.json
    name = data['name']
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('UPDATE books SET name = %s WHERE id = %s RETURNING id, name;', (name, id))
    book = cur.fetchone()
    conn.commit()
    
    if book:
        # Invalidate cache and publish event
        invalidate_books_cache()
        publish_book_event('book_updated', {'id': book[0], 'name': book[1]})
    
    cur.close()
    conn.close()
    return jsonify({'message': 'Book updated', 'book': book})

@app.route('/books/<int:id>', methods=['DELETE'])
def delete_book(id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get book before deletion for event
        cur.execute('SELECT id, name FROM books WHERE id = %s;', (id,))
        book = cur.fetchone()
        
        if not book:
            return jsonify({'error': 'Book not found'}), 404
            
        # Delete the book
        cur.execute('DELETE FROM books WHERE id = %s;', (id,))
        conn.commit()
        
        # Verify deletion
        cur.execute('SELECT id FROM books WHERE id = %s;', (id,))
        if cur.fetchone():
            raise Exception('Book deletion failed')
            
        # Invalidate cache and publish event
        invalidate_books_cache()
        publish_book_event('book_deleted', {'id': book[0], 'name': book[1]})
            
        cur.close()
        conn.close()
        
        return jsonify({'message': f'Book with id {id} deleted successfully'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)