set output_name analog_psfxl
database -open waves -into $output_name -default
set scope native_tb
probe -create -database waves $scope -all -depth all
run 20ns
run 60ns
exit
