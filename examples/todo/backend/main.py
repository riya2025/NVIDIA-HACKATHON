"""Tiny To-Do API (FastAPI, in-memory store).

Run with:  uvicorn main:app --reload --port 8090
"""
from __future__ import annotations

from itertools import count

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Quick To-Do API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class Todo(BaseModel):
    id: int
    title: str
    done: bool = False


class TodoCreate(BaseModel):
    title: str


_ids = count(1)
_todos: dict[int, Todo] = {}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/todos")
def list_todos() -> list[Todo]:
    return list(_todos.values())


@app.post("/todos", status_code=201)
def create_todo(payload: TodoCreate) -> Todo:
    title = payload.title.strip()
    if not title:
        raise HTTPException(422, "title must not be empty")
    todo = Todo(id=next(_ids), title=title)
    _todos[todo.id] = todo
    return todo


@app.patch("/todos/{todo_id}")
def toggle_todo(todo_id: int) -> Todo:
    todo = _todos.get(todo_id)
    if not todo:
        raise HTTPException(404, "todo not found")
    todo.done = not todo.done
    return todo


@app.delete("/todos/{todo_id}", status_code=204)
def delete_todo(todo_id: int) -> None:
    if todo_id not in _todos:
        raise HTTPException(404, "todo not found")
    del _todos[todo_id]
