import sqlite3
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from contextlib import asynccontextmanager

DB_FILE = "data.db"

# データベースの初期化
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON;")
    cursor = conn.cursor()
    
    # 既存の daily_reports のカラムをチェックしてマイグレーションまたは再作成
    cursor.execute("PRAGMA table_info(daily_reports)")
    columns = [row[1] for row in cursor.fetchall()]
    
    # もし古いカラム（waste_bread 等）が存在していれば、古いテーブルを削除して再作成する
    if "waste_bread" in columns or "waste_bento" in columns:
        cursor.execute("DROP TABLE IF EXISTS daily_product_records")
        cursor.execute("DROP TABLE IF EXISTS daily_reports")
        cursor.execute("DROP TABLE IF EXISTS products")
        
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_reports (
            date TEXT PRIMARY KEY,
            weather TEXT,
            customers INTEGER,
            sales INTEGER
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            is_active INTEGER DEFAULT 1
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_product_records (
            date TEXT,
            product_id INTEGER,
            prepared INTEGER,
            wasted INTEGER,
            PRIMARY KEY (date, product_id),
            FOREIGN KEY (date) REFERENCES daily_reports(date) ON DELETE CASCADE,
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """)
    
    conn.commit()
    conn.close()

# アプリケーションのライフサイクル管理
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Daily Sales & Product Report API", lifespan=lifespan)

# CORSの設定 (フロントエンドからのアクセスを許可)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静的ファイルの配信設定 (HTMLファイルを http://localhost:8000/ で直接アクセス可能にする)
app.mount("/", StaticFiles(directory=".", html=True), name="static")

# --- Pydanticモデルの定義 ---

# 商品管理用
class ProductCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="商品名 (1〜100文字)")

class ProductResponse(BaseModel):
    id: int
    name: str
    is_active: int

    class Config:
        from_attributes = True

# 営業データ & 商品別実績用
class DailyProductRecordInput(BaseModel):
    product_id: int = Field(..., description="商品ID", ge=1)
    prepared: int = Field(..., description="仕込み数", ge=0)
    wasted: int = Field(..., description="廃棄数", ge=0)

class DailyReportInput(BaseModel):
    date: str = Field(..., description="日付 (例: '2026-06-02')", pattern=r"^\d{4}-\d{2}-\d{2}$")
    weather: str = Field(..., description="天気 (例: 'sunny')")
    customers: int = Field(..., description="客数", ge=0)
    sales: int = Field(..., description="売上", ge=0)
    products: List[DailyProductRecordInput] = Field(default=[], description="商品別実績リスト")

class DailyProductRecordResponse(BaseModel):
    product_id: int
    name: str
    prepared: int
    wasted: int

class DailyReportResponse(BaseModel):
    date: str
    weather: str
    customers: int
    sales: int
    products: List[DailyProductRecordResponse]


# --- APIエンドポイントの実装 ---

# 1. 商品管理 (Products CRUD)

@app.get("/api/products", response_model=List[ProductResponse])
def get_products():
    """有効な商品(is_active = 1)のリストを取得します。"""
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, is_active FROM products WHERE is_active = 1")
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@app.post("/api/products", response_model=ProductResponse, status_code=201)
def create_product(product: ProductCreate):
    """新規商品を登録します。既に同じ名前の商品が存在し無効化されている場合は有効化します。"""
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # 同名商品の存在チェック
        cursor.execute("SELECT id, name, is_active FROM products WHERE name = ?", (product.name,))
        existing = cursor.fetchone()
        
        if existing:
            if existing["is_active"] == 0:
                # 無効化されている場合は有効(1)に復元
                cursor.execute("UPDATE products SET is_active = 1 WHERE id = ?", (existing["id"],))
                conn.commit()
                cursor.execute("SELECT id, name, is_active FROM products WHERE id = ?", (existing["id"],))
                updated = cursor.fetchone()
                conn.close()
                return dict(updated)
            else:
                # 既に有効な同名商品が存在する場合はエラー
                conn.close()
                raise HTTPException(status_code=400, detail="Product with this name already exists and is active")
        
        # 新規登録
        cursor.execute("INSERT INTO products (name) VALUES (?)", (product.name,))
        new_id = cursor.lastrowid
        conn.commit()
        
        cursor.execute("SELECT id, name, is_active FROM products WHERE id = ?", (new_id,))
        new_product = cursor.fetchone()
        conn.close()
        return dict(new_product)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@app.delete("/api/products/{id}", status_code=200)
def delete_product(id: int):
    """指定された商品を論理削除(is_active = 0)します。"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # 商品の存在チェック
        cursor.execute("SELECT id, is_active FROM products WHERE id = ?", (id,))
        existing = cursor.fetchone()
        if not existing:
            conn.close()
            raise HTTPException(status_code=404, detail="Product not found")
        
        if existing[1] == 0:
            conn.close()
            return {"message": "Product is already inactive", "id": id}
            
        cursor.execute("UPDATE products SET is_active = 0 WHERE id = ?", (id,))
        conn.commit()
        conn.close()
        return {"message": "Product logic-deleted successfully", "id": id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


# 2. 営業データ (Reports)

@app.post("/api/reports", status_code=201)
def create_or_update_report(report: DailyReportInput):
    """
    営業日報と商品別実績を保存します。
    トランザクション処理により、全体の営業実績と商品別実績を整合的に保存します。
    """
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON;")
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN TRANSACTION;")
        
        # 1. daily_reports に保存 (INSERT OR REPLACE)
        cursor.execute("""
            INSERT OR REPLACE INTO daily_reports (date, weather, customers, sales)
            VALUES (?, ?, ?, ?)
        """, (report.date, report.weather, report.customers, report.sales))
        
        # 2. その日の既存の商品別実績を削除
        cursor.execute("DELETE FROM daily_product_records WHERE date = ?", (report.date,))
        
        # 3. 新しい商品別実績を挿入
        for p_record in report.products:
            # 外部キー制約エラーを避けるために商品IDの存在チェック
            cursor.execute("SELECT id FROM products WHERE id = ?", (p_record.product_id,))
            prod = cursor.fetchone()
            if not prod:
                raise HTTPException(status_code=400, detail=f"Product with id {p_record.product_id} does not exist")
                
            cursor.execute("""
                INSERT INTO daily_product_records (date, product_id, prepared, wasted)
                VALUES (?, ?, ?, ?)
            """, (report.date, p_record.product_id, p_record.prepared, p_record.wasted))
            
        conn.commit()
        return {"message": "Report saved successfully", "date": report.date}
    except HTTPException as e:
        conn.rollback()
        raise e
    except sqlite3.IntegrityError as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"Database integrity error: {str(e)}")
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        conn.close()

@app.get("/api/reports", response_model=List[DailyReportResponse])
def get_reports(month: Optional[str] = Query(None, description="月指定フィルタ (例: '2026-06')")):
    """
    営業データの一覧を取得します。
    monthパラメータで月指定(例: '2026-06')フィルタが可能です。
    """
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        if month:
            query = """
                SELECT 
                    dr.date, dr.weather, dr.customers, dr.sales,
                    dpr.product_id, p.name AS product_name, dpr.prepared, dpr.wasted
                FROM daily_reports dr
                LEFT JOIN daily_product_records dpr ON dr.date = dpr.date
                LEFT JOIN products p ON dpr.product_id = p.id
                WHERE dr.date LIKE ?
                ORDER BY dr.date ASC, dpr.product_id ASC
            """
            cursor.execute(query, (f"{month}%",))
        else:
            query = """
                SELECT 
                    dr.date, dr.weather, dr.customers, dr.sales,
                    dpr.product_id, p.name AS product_name, dpr.prepared, dpr.wasted
                FROM daily_reports dr
                LEFT JOIN daily_product_records dpr ON dr.date = dpr.date
                LEFT JOIN products p ON dpr.product_id = p.id
                ORDER BY dr.date ASC, dpr.product_id ASC
            """
            cursor.execute(query)
            
        rows = cursor.fetchall()
        conn.close()
        
        # データをネスト構造に整形
        reports_dict = {}
        for row in rows:
            date = row["date"]
            if date not in reports_dict:
                reports_dict[date] = {
                    "date": date,
                    "weather": row["weather"],
                    "customers": row["customers"],
                    "sales": row["sales"],
                    "products": []
                }
            if row["product_id"] is not None:
                reports_dict[date]["products"].append({
                    "product_id": row["product_id"],
                    "name": row["product_name"],
                    "prepared": row["prepared"],
                    "wasted": row["wasted"]
                })
                
        return list(reports_dict.values())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
