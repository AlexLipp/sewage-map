# Local raw EIR data

Raw company data is intentionally excluded from Git. Obtain the source files
independently and place them under the matching folder:

```text
anglian/
northumbria/
severn_trent/
south_west_water/
southern_water/
united_utilities/
wessex/
yorkshire/
```

Nested folders are allowed. The cleaners read these files without modifying
them and rebuild the corresponding CSV under
`clean_EIR_stopstartdata/input_stopstart_data/`. Do not use `git add -f` to
commit raw files.
