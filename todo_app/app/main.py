from fastapi import FastAPI, Depends, HTTPException, status
from contextlib import asynccontextmanager
from app.db import create_db_and_tables, get_session
from app.router import user
from typing import Annotated
from app.models import Todo_Create, Todo_Edit, Token, User, Todo
from fastapi.security import OAuth2PasswordRequestForm
from sqlmodel import Session, select
from datetime import timedelta
from app.auth import EXPIRY_TIME, authenticate_user, create_access_token, current_user, validate_refresh_token, create_refresh_token
from aiokafka import AIOKafkaProducer, AIOKafkaConsumer
import asyncio
import json
from app import todo_pb2

async def consume_messages(topic, bootstrap_servers):
    # Create a consumer instance.
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap_servers,
        group_id="my-group",
        auto_offset_reset='earliest'
    )
    # Start the consumer.
    await consumer.start()
    try:
        # Continuously listen for messages.
        async for message in consumer:
            print(f"Received message: {message.value.decode()} on topic {message.topic}")
            new_todo = todo_pb2.Todo()
            new_todo.ParseFromString(message.value)
            print(f"Consumer Deserialized data: {new_todo}")
            # Here you can add code to process each message.
            # Example: parse the message, store it in a database, etc.
    finally:
        # Ensure to close the consumer when done.
        await consumer.stop()


# The first part of the function, before the yield, will
# be executed before the application starts.
# https://fastapi.tiangolo.com/advanced/events/#lifespan-function
# loop = asyncio.get_event_loop()

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Creating tables..")
    task = asyncio.create_task(consume_messages('todos', 'broker:19092'))
    create_db_and_tables()
    yield


app = FastAPI(lifespan=lifespan, title="Todo API", 
    version="0.0.1",
    servers=[
        {
            "url": "http://127.0.0.1:8000", # ADD NGROK URL Here Before Creating GPT Action
            "description": "Development Server"
        }
        ])

app.include_router(router=user.user_router)

# 01 Define the route to read route
@app.get("/")
def read_root():
    return {"Welcome": "Todo API"}

# login . username, password
@app.post('/token', response_model=Token)
async def login(form_data:Annotated[OAuth2PasswordRequestForm, Depends()],
                session:Annotated[Session, Depends(get_session)]):
    user = authenticate_user (form_data.username, form_data.password, session)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    
    expire_time = timedelta(minutes=EXPIRY_TIME)
    access_token = create_access_token({"sub":form_data.username}, expire_time)

    refresh_expire_time = timedelta(days=7)
    refresh_token = create_refresh_token({"sub":user.email}, refresh_expire_time)

    return Token(access_token=access_token, token_type="bearer", refresh_token=refresh_token)


@app.post("/token/refresh")
def refresh_token(old_refresh_token:str,
                  session:Annotated[Session, Depends(get_session)]):
    
    credential_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid token, Please login again",
        headers={"www-Authenticate":"Bearer"}
    )
    
    user = validate_refresh_token(old_refresh_token,
                                  session)
    if not user:
        raise credential_exception
    
    expire_time = timedelta(minutes=EXPIRY_TIME)
    access_token = create_access_token({"sub":user.username}, expire_time)

    refresh_expire_time = timedelta(days=7)
    refresh_token = create_refresh_token({"sub":user.email}, refresh_expire_time)

    return Token(access_token=access_token, token_type= "bearer", refresh_token=refresh_token)

# Kafka Producer as a dependency
async def get_kafka_producer():
    producer = AIOKafkaProducer(bootstrap_servers='broker:19092')
    await producer.start()
    try:
        yield producer
    finally:
        await producer.stop()

@app.post('/todos/', response_model=Todo)
async def create_todo(current_user:Annotated[User, Depends(current_user)],
                      todo: Todo_Create, 
                      session: Annotated[Session, Depends(get_session)],
                      producer: Annotated[AIOKafkaProducer, Depends(get_kafka_producer)]):
    
    new_todo = Todo(content=todo.content, user_id=current_user.id)
  
    todo_protbuf = todo_pb2.Todo(content=todo.content)
    print(f"Todo Protobuf: {todo_protbuf}")
    # Serialize the message to a byte string
    serialized_todo = todo_protbuf.SerializeToString()
    print(f"Serialized data: {serialized_todo}")
    # Produce message
    await producer.send_and_wait("todos", serialized_todo)
    session.add(new_todo)
    session.commit()
    session.refresh(new_todo)
    return new_todo


@app.get('/todos/', response_model=list[Todo])
async def get_all(current_user:Annotated[User, Depends(current_user)],
                  session: Annotated[Session, Depends(get_session)]):  
    todos = session.exec(select(Todo).where(Todo.user_id == current_user.id)).all()

    if todos:
        return todos
    else:
        raise HTTPException(status_code=404, detail="No Task found")


@app.get('/todos/{id}', response_model=Todo)
async def get_single_todo(id: int, 
                          current_user:Annotated[User, Depends(current_user)],
                          session: Annotated[Session, Depends(get_session)]):
    
    user_todos = session.exec(select(Todo).where(Todo.user_id == current_user.id)).all()
    matched_todo = next((todo for todo in user_todos if todo.id == id),None)

    if matched_todo:
        return matched_todo
    else:
        raise HTTPException(status_code=404, detail="No Task found")


@app.put('/todos/{id}')
async def edit_todo(id: int, 
                    todo: Todo_Edit,
                    current_user:Annotated[User, Depends(current_user)], 
                    session: Annotated[Session, Depends(get_session)]):
    
    user_todos = session.exec(select(Todo).where(Todo.user_id == current_user.id)).all()
    existing_todo = next((todo for todo in user_todos if todo.id == id),None)

    if existing_todo:
        existing_todo.content = todo.content
        existing_todo.is_completed = todo.is_completed
        session.add(existing_todo)
        session.commit()
        session.refresh(existing_todo)
        return existing_todo
    else:
        raise HTTPException(status_code=404, detail="No task found")


@app.delete('/todos/{id}')
async def delete_todo(id: int,
                      current_user:Annotated[User, Depends(current_user)],
                      session: Annotated[Session, Depends(get_session)]):
    
    user_todos = session.exec(select(Todo).where(Todo.user_id == current_user.id)).all()
    todo = next((todo for todo in user_todos if todo.id == id),None)
    
    if todo:
        session.delete(todo)
        session.commit()
        # session.refresh(todo)
        return {"message": "Task successfully deleted"}
    else:
        raise HTTPException(status_code=404, detail="No task found")