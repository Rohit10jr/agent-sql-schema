# Django REST Auth and LangChain LangGraph Exploration

This project is an exploration of advanced concepts using `dj-rest-auth` for authentication in Django projects and `langchain` with `langgraph` for building complex, stateful LLM applications.

## Getting Started

To get started with this project, you'll need to have Python and pip installed.

1.  **Clone the repository:**
    ```bash
    git clone <repository-url>
    cd backend
    ```

2.  **Create a virtual environment and activate it:**
    ```bash
    python -m venv venv
    .\venv\Scripts\activate
    ```

3.  **Install the dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Set up the environment variables:**
    Create a `.env` file in the project root and add the necessary environment variables. See the `.env.example` for reference.

5.  **Run the database migrations:**
    ```bash
    python manage.py migrate
    ```

6.  **Start the development server:**
    ```bash
    python manage.py runserver
    ```

## Features

*   **Authentication:** Utilizes `dj-rest-auth` for a robust and secure authentication system.
*   **LLM Integration:** Explores `langchain` and `langgraph` to create and manage conversational AI agents and graphs.
*   **Streaming:** Implements streaming responses for real-time communication with the backend.
