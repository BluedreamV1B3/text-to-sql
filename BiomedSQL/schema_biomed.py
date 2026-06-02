databaseSchema = {
    "__normalized_from": "bio_sql_benchmark",
    "tables": {

        # =====================================================
        # Alzheimer GWAS
        # =====================================================
        "AlzheimerDisease_CombinedGeneData_UUID": {
            "pk": ["UUID"],
            "fk": {},
            "cols": {
                "UUID": "text",
                "SNP": "text",
                "A1": "text",
                "A2": "text",
                "freq": "number",
                "b": "number",
                "se": "number",
                "p": "number",
                "chr_37": "number",
                "bp_37": "number",
                "chr_38": "number",
                "bp_38": "number",
                "nearestGene": "text"
            },
            "enums": {}
        },

        # =====================================================
        # Parkinson GWAS
        # =====================================================
        "ParkinsonDisease_CompleteGeneData_No23andMe": {
            "pk": ["UUID"],
            "fk": {},
            "cols": {
                "UUID": "text",
                "SNP": "text",
                "A1": "text",
                "A2": "text",
                "freq": "number",
                "b": "number",
                "se": "number",
                "p": "number",
                "chr_37": "number",
                "bp_37": "number",
                "chr_38": "number",
                "bp_38": "number",
                "nearestGene": "text"
            },
            "enums": {}
        },

        # =====================================================
        # SMR Results
        # =====================================================
        "NeurodegenerativeDiseases_SMR_Genes_Full": {
            "pk": ["UUID"],
            "fk": {},
            "cols": {
                "UUID": "text",
                "Omic": "text",
                "Disease": "text",
                "probeID": "text",
                "ProbeChr": "number",
                "Gene": "text",
                "Probe_bp": "number",
                "topSNP": "text",
                "topSNP_chr": "number",
                "topSNP_bp": "number",
                "A1": "text",
                "A2": "text",
                "Freq": "number",
                "b_GWAS": "number",
                "se_GWAS": "number",
                "p_GWAS": "number",
                "b_eQTL": "number",
                "se_eQTL": "number",
                "p_eQTL": "number",
                "b_SMR": "number",
                "se_SMR": "number",
                "p_SMR": "number",
                "p_SMR_multi": "number",
                "p_HEIDI": "number",
                "nsnp_HEIDI": "number",
                "topRSID": "text",
                "Omic_type": "text",
                "Omic_tissue": "text",
                "Disease_name": "text",
                "Source": "text",
                "func_sig": "text"
            },
            "enums": {}
        },

        # =====================================================
        # Drug Master Table
        # =====================================================
        "DrugGeneTargets_ComprehensiveAnnotations_updated": {
            "pk": ["UUID"],
            "fk": {},
            "cols": {
                "UUID": "text",
                "chemblIdentifier": "text",
                "blackBoxWarning": "boolean",
                "drugName": "text",
                "drugMolecularType": "text",
                "yearOfFirstApproval": "number",
                "maxClinicalTrialPhase": "number",
                "drugHasBeenWithdrawn": "boolean",
                "drugIsApproved": "boolean",
                "tradeNames_string": "text",
                "drugSynonyms_string": "text",
                "linkedDiseasesDrug_string": "text",
                "linkedDiseasesCount": "number",
                "newLinkedTargets_string": "text",
                "numberLinkedTargets": "number",
                "drugDescription": "text",
                "drugActionType": "text",
                "drugMechanismOfAction": "text",
                "tradename_count": "number",
                "synonyms_count": "number"
            },
            "enums": {}
        },

        # =====================================================
        # Drug Indications 
        # =====================================================
        "DrugTargets_IndicationsAndTherapeuticUses": {
            "pk": ["UUID"],
            "fk": {},
            "cols": {
                "UUID": "text",
                "chemblId": "text",
                "drugName": "text",
                "tradeName": "text",
                "drugType": "text",
                "actionType": "text",
                "targetType": "text",
                "target": "text",
                "approvedSymbol": "text",
                "approvedName": "text",
                "yearOfFirstApproval": "number",
                "usan_year": "number",
                "patent_no": "text",
                "max_phase_for_ind": "number",
                "mesh_id": "text",
                "mesh_heading": "text",
                "efo_id": "text",
                "efo_term": "text",
                "tradeNames_list": "text",
                "tradename_count": "number",
                "syns_list": "text",
                "synonyms_count": "number",
                "ct": "text"
            },
            "enums": {}
        }

    }
}
