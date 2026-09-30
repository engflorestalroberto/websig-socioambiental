import os
import tempfile
import shutil
import geopandas as gpd
import fiona
import pandas as pd
import re
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import json
import warnings

warnings.filterwarnings("ignore")
fiona.drvsupport.supported_drivers['KML'] = 'rw'
fiona.drvsupport.supported_drivers['LIBKML'] = 'rw'

app = FastAPI(
    title="WebSIG Ambiental API",
    version="2.3.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PATH_AMAZONIA = r"D:\Base_Dados\yearly_deforestation_biome_amazonia_v20260330"
PATH_CERRADO = r"D:\Base_Dados\yearly_deforestation_biome_cerrado_v20260330"
PATH_MUNICIPIOS = r"D:\Base_Dados\BR_Municipios_2025"
PATH_QUILOMBOLAS = r"D:\CAR\Quilombolas"
PATH_UCS = r"D:\CAR\Unidades_Conservacao" 
PATH_IBAMA = r"D:\CAR\adm_embargo_ibama_a.shp"
PATH_TRABALHO_ESCRAVO = r"C:\Users\rober\Downloads\cadastro_de_empregadores.csv"

def checar_trabalho_escravo(documento):
    if not documento or not os.path.exists(PATH_TRABALHO_ESCRAVO):
        return {"status": "NÃO VERIFICADO", "mensagem": "Documento não analisado."}
    
    doc_limpo = re.sub(r'[^0-9]', '', documento)
    if not doc_limpo or len(doc_limpo) < 11:
        return {"status": "INVÁLIDO", "mensagem": "Documento inválido."}
        
    try:
        try:
            df = pd.read_csv(PATH_TRABALHO_ESCRAVO, sep=';', encoding='latin1', dtype=str)
        except:
            df = pd.read_csv(PATH_TRABALHO_ESCRAVO, sep=',', encoding='utf-8', dtype=str)

        for index, row in df.iterrows():
            for val in row.values:
                val_limpo = re.sub(r'[^0-9]', '', str(val))
                if val_limpo == doc_limpo:
                    detalhes = {k: v for k, v in row.to_dict().items() if pd.notna(v)}
                    return {
                        "status": "ALERTA VERMELHO",
                        "mensagem": "Consta na Lista Suja do Trabalho Escravo do MTE!",
                        "versao_lista": "28/09/2026",
                        "dados_infracao": detalhes
                    }
        return {"status": "REGULAR", "mensagem": "Nada consta na Lista Suja.", "versao_lista": "28/09/2026"}
    except Exception as e:
        return {"status": "ERRO", "mensagem": f"Erro: {str(e)}"}

def cruzar_base_otimizada(fazenda_gdf, caminho, apenas_poligonos=False):
    if not os.path.exists(caminho): return None
    try:
        with fiona.open(caminho, "r") as src:
            crs_base = src.crs
        if not crs_base: crs_base = "EPSG:4674"
            
        fazenda_convertida = fazenda_gdf.to_crs(crs_base)
        recorte = gpd.read_file(caminho, mask=fazenda_convertida)
        
        if recorte.empty: return None
        if apenas_poligonos:
            recorte = recorte[recorte.geometry.type.isin(['Polygon', 'MultiPolygon'])]
            if recorte.empty: return None
                
        interseccao = gpd.overlay(recorte, fazenda_convertida, how='intersection')
        if interseccao.empty: return None
            
        interseccao = interseccao.to_crs(epsg=4326)
        inter_proj = interseccao.to_crs(epsg=5880)
        interseccao['area_ha'] = inter_proj.geometry.area / 10000
        interseccao = interseccao[interseccao['area_ha'] > 0.001] 
        
        return interseccao
    except Exception as e:
        print(f"Erro ao ler {caminho}: {e}")
        return None

def buscar_nome_coluna(df):
    colunas = [c.lower() for c in df.columns]
    for p in ['nm_mun', 'nome', 'nom_uc', 'nome_area', 'nm_comunid', 'name']:
        for c in colunas:
            if p in c: return df.columns[colunas.index(c)]
    return None

def extrair_features_com_filtro(gdf, props_base, ano_corte=None):
    features = json.loads(gdf.to_json())['features']
    features_filtradas = []
    
    for i, (_, row) in enumerate(gdf.iterrows()):
        props = props_base.copy()
        props['area_ha'] = round(row['area_ha'], 4)
        incluir = True
        
        if 'ano' in props: # Se for PRODES
            colunas = [c.lower() for c in gdf.columns]
            ano_str = "N/A"
            if 'ano' in colunas: ano_str = row.iloc[colunas.index('ano')]
            elif 'year' in colunas: ano_str = row.iloc[colunas.index('year')]
            elif 'view_date' in colunas: ano_str = row.iloc[colunas.index('view_date')]
            
            # FILTRO: Data de Corte (Moratória da Soja / Código Florestal)
            if ano_corte is not None and str(ano_str).isdigit():
                if int(ano_str) < int(ano_corte):
                    incluir = False # Ignora esse desmatamento!
                    
            props['ano'] = str(ano_str)
            
        if incluir:
            feat = features[i]
            feat['properties'] = props
            features_filtradas.append(feat)
            
    return features_filtradas

def gerar_relatorio_socioambiental(fazenda_gdf, ano_corte=None):
    relatorio = {
        "localizacao": {"municipios": []},
        "alertas_sobreposicao": {
            "desmatamento_prodes_ha": 0.0,
            "terras_quilombolas_ha": 0.0,
            "unidades_conservacao_ha": 0.0,
            "embargos_ibama_ha": 0.0
        },
        "detalhes_desmatamento": [],
        "geometrias_alerta": {
            "prodes": [], "quilombolas": [], "ucs": [], "ibama": []
        }
    }
    
    mun_df = cruzar_base_otimizada(fazenda_gdf, PATH_MUNICIPIOS)
    if mun_df is not None and not mun_df.empty:
        col_nome = buscar_nome_coluna(mun_df)
        for _, row in mun_df.iterrows():
            if col_nome:
                nome = str(row[col_nome])
                if nome not in relatorio["localizacao"]["municipios"]:
                    relatorio["localizacao"]["municipios"].append(nome)
            
    for bioma, path in [("Amazônia", PATH_AMAZONIA), ("Cerrado", PATH_CERRADO)]:
        prodes_df = cruzar_base_otimizada(fazenda_gdf, path)
        if prodes_df is not None and not prodes_df.empty:
            # Chama a função de extração passando o ano_corte!
            features = extrair_features_com_filtro(prodes_df, {"tipo": "PRODES", "bioma": bioma, "ano": ""}, ano_corte)
            relatorio["geometrias_alerta"]["prodes"].extend(features)
            for f in features:
                relatorio["alertas_sobreposicao"]["desmatamento_prodes_ha"] += f['properties']['area_ha']
                relatorio["detalhes_desmatamento"].append({"bioma": f['properties']['bioma'], "ano": f['properties']['ano'], "area_ha": f['properties']['area_ha']})

    relatorio["alertas_sobreposicao"]["desmatamento_prodes_ha"] = round(relatorio["alertas_sobreposicao"]["desmatamento_prodes_ha"], 4)

    qui_df = cruzar_base_otimizada(fazenda_gdf, PATH_QUILOMBOLAS)
    if qui_df is not None and not qui_df.empty:
        features = extrair_features_com_filtro(qui_df, {"tipo": "Quilombola"})
        relatorio["geometrias_alerta"]["quilombolas"].extend(features)
        relatorio["alertas_sobreposicao"]["terras_quilombolas_ha"] = round(qui_df['area_ha'].sum(), 4)
        
    uc_df = cruzar_base_otimizada(fazenda_gdf, PATH_UCS, apenas_poligonos=True)
    if uc_df is not None and not uc_df.empty:
        features = extrair_features_com_filtro(uc_df, {"tipo": "Unidade de Conservação"})
        relatorio["geometrias_alerta"]["ucs"].extend(features)
        relatorio["alertas_sobreposicao"]["unidades_conservacao_ha"] = round(uc_df['area_ha'].sum(), 4)
        
    ibama_df = cruzar_base_otimizada(fazenda_gdf, PATH_IBAMA)
    if ibama_df is not None and not ibama_df.empty:
        features = extrair_features_com_filtro(ibama_df, {"tipo": "Embargo IBAMA"})
        relatorio["geometrias_alerta"]["ibama"].extend(features)
        relatorio["alertas_sobreposicao"]["embargos_ibama_ha"] = round(ibama_df['area_ha'].sum(), 4)

    return relatorio

@app.get("/")
def read_root(): return {"mensagem": "API Rodando!"}

@app.post("/analise/upload-poligono/")
async def upload_poligono(
    arquivo: UploadFile = File(...), 
    cpf_cnpj: str = Form(None),
    ano_corte: int = Form(None) # Novo Campo!
):
    extensao = arquivo.filename.split('.')[-1].lower()
    if extensao not in ['zip', 'kml', 'kmz', 'geojson']: raise HTTPException(status_code=400, detail="Formato não suportado.")

    with tempfile.TemporaryDirectory() as temp_dir:
        caminho_arquivo = os.path.join(temp_dir, arquivo.filename)
        with open(caminho_arquivo, "wb") as buffer:
            shutil.copyfileobj(arquivo.file, buffer)
        try:
            gdf = gpd.read_file(f"zip://{caminho_arquivo}" if extensao == 'zip' else caminho_arquivo)
            if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
                gdf = gdf.to_crs(epsg=4326)
            
            geometria_unida = gdf.geometry.unary_union
            gdf_unido = gpd.GeoDataFrame(geometry=[geometria_unida], crs="EPSG:4326")
            
            resultado_espacial = gerar_relatorio_socioambiental(gdf_unido, ano_corte)
            resultado_trabalho_escravo = checar_trabalho_escravo(cpf_cnpj)
            
            geometria_str = json.dumps(gdf_unido.__geo_interface__["features"][0]["geometry"])
            area_fazenda_ha = round(gdf_unido.to_crs(epsg=5880).geometry.area.sum() / 10000, 2)
            
            return {
                "relatorio_socioambiental": {
                    "documento_consultado": cpf_cnpj,
                    "ano_corte_prodes": ano_corte,
                    "trabalho_escravo": resultado_trabalho_escravo,
                    "imovel": {
                        "nome_arquivo": arquivo.filename,
                        "area_imovel_ha": area_fazenda_ha,
                        "municipios": resultado_espacial["localizacao"]["municipios"]
                    },
                    "resumo_alertas_espaciais": resultado_espacial["alertas_sobreposicao"],
                    "geometrias_alerta": resultado_espacial["geometrias_alerta"],
                    "detalhes_desmatamento": resultado_espacial["detalhes_desmatamento"]
                },
                "geometria_do_mapa": json.loads(geometria_str)
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Erro: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
