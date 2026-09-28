import os
from decimal import Decimal


class Settings:
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./pmcontrol.db")
    JWT_SECRET: str = os.getenv("JWT_SECRET", "dev-secret-change-me-in-production")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = int(os.getenv("JWT_EXPIRE_MINUTES", "480"))
    PASSWORD_ITERATIONS: int = int(os.getenv("PASSWORD_ITERATIONS", "310000"))
    DEFAULT_STANDARD_HOURS: Decimal = Decimal(os.getenv("STANDARD_HOURS_PER_DAY", "8"))
    CORS_ORIGINS: list[str] = os.getenv("CORS_ORIGINS", "*").split(",")


settings = Settings()
